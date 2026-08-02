"""
Orchestrator — Central hub for the multi-agent system.

Responsibilities:
1. WebSocket server for UI clients (/ws) and agent connections
2. A2A agent discovery via agent cards
3. LLM-powered tool routing (chat message → tool selection)
4. Parallel MCP tool execution across agents
5. Dynamic UI assembly (combines tool outputs into cohesive layouts)
"""
import asyncio
import contextvars
import hashlib
import json
import time
import os
import random
import sys
import logging
import re
from contextlib import asynccontextmanager
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, NamedTuple, Optional

import websockets
import websockets.exceptions
import aiohttp
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from jose import jwt as jose_jwt
from dotenv import load_dotenv

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from orchestrator.history import (
    ConversationCommitRepository,
    ConversationNotFound,
    HistoryManager,
    augment_conversation_snapshot_for_target,
)
from orchestrator.tool_permissions import ToolPermissionManager
from orchestrator.credential_manager import CredentialManager
from orchestrator.delegation import DelegationService
from orchestrator.tool_security import ToolSecurityAnalyzer
from orchestrator.compaction import compact_messages
from orchestrator import context_engineering
from orchestrator import datamarking
from orchestrator import model_router
from orchestrator import viewport
from orchestrator.hooks import HookManager, HookEvent, HookContext
from orchestrator.task_state import TaskManager, TaskState
from orchestrator.work_admission import (
    AdmissionClass,
    ExecutionFence,
    InMemoryWorkAdmissionRepository,
    OperationOwner,
    OperationRecord,
    OperationRequest,
    OperationState,
    OwnerScope,
    SafeOperationProjection,
    StaleExecutionFenceError,
    WorkAdmissionCoordinator,
)
from orchestrator.runtime_observability import RuntimeObservability
from orchestrator.concurrency_cap import ConcurrencyCap
from orchestrator.voice_control_binding import (
    VoiceControlBindingError,
    VoiceControlBindingIssuer,
    VoiceControlClaims,
)

import uuid as _uuid

if TYPE_CHECKING:
    from scheduler.store import ScheduledAttempt, ScheduledJobStore

from shared.protocol import (
    Message, MCPRequest, MCPResponse, UIEvent, UIRender, UIUpdate,
    RegisterAgent, RegisterUI, AgentCard, ToolProgress,
    AgentHostRegistration, AgentHostRegistered, CandidateCapabilityMap,
    AGENT_LIFECYCLE_REASON_CODES, ProtocolValidationError, RuntimeFence,
    MCP_PROTOCOL_VERSION,
    ChatCreated,
    ConversationCommitReady,
    CorrelatedNewChat,
    VoiceControlBinding,
    VoicePlayoutEvent,
    ToolStreamData, ToolStreamEnd, ToolStreamCancel,
    AgentHopRequest, AgentHopResponse,
    validate_streaming_metadata,
)
from astralprims import (
    Text, Card, Alert, Button, Collapsible
)
from rote.rote import ROTE
from shared.feature_flags import flags
from shared.llm_text import strip_reasoning_markup
from shared.perf import perf_span
from orchestrator.stream_manager import StreamManager, markdown_safe_prefix_len

load_dotenv(override=False)

PORT = int(os.getenv("ORCHESTRATOR_PORT", 8001))

# Feature 060 / T025: connection-runtime policy.  These are module constants so
# operators and the contract suite can inspect the exact production defaults.
REGISTRATION_TIMEOUT_SECONDS = 5.0
CONNECTION_DRAIN_TIMEOUT_SECONDS = 5.0
REGISTRATION_QUEUE_LIMIT = 16
CONNECTION_INGRESS_LIMIT = 4096
CONNECTION_LEASE_RENEW_SECONDS = 5.0
# The status contract requires the current phase to be visible once an
# operation has remained active for one second.  The client-local
# ``submitting`` projection covers the pre-admission interval; this timer owns
# the first durable server phase.
OPERATION_PROGRESS_PHASE_SECONDS = 1.0
_CONNECTION_CLAIM_POLL_SECONDS = 0.25
# The connection-operation context is runner-local and has no re-entry after
# terminal cleanup returns.  One immediate exact-authority retry absorbs a
# transient repository read without creating an orphaned background finalizer.
_VOICE_TERMINAL_FINALIZATION_ATTEMPTS = 2
_VOICE_REQUEST_FAILED_MESSAGE = (
    "Voice request failed. This request did not complete. Review the error in "
    "the conversation, then try again; typed chat is still available."
)
_VOICE_REQUEST_INTERRUPTED_MESSAGE = (
    "Voice request interrupted. This request did not complete. Please try "
    "again; typed chat is still available."
)
_VOICE_REQUEST_CANCELLED_MESSAGE = (
    "Voice request cancelled. No completed result was produced. You can try "
    "again or keep using typed chat."
)
_VOICE_RESULT_UNAVAILABLE_MESSAGE = (
    "The request finished processing, but its result could not be published. "
    "Please try again or keep using typed chat."
)
_VOICE_REQUEST_SUCCEEDED_MESSAGE = (
    "Request completed. The text result is available in the conversation."
)
_VOICE_REQUEST_PROCESSING_MESSAGE = (
    "Voice request accepted. Working on it; typed chat remains available."
)


class _SafeLLMErrorMetadata(NamedTuple):
    """Content-free provider failure facts safe for logs and audit routing."""

    exception_class: str
    status_code: Optional[int]
    upstream_error_class: str
    retryable: bool


class _LLMHTMLMaintenanceError(RuntimeError):
    """A successful HTTP envelope carried an upstream maintenance page."""


class _LLMMalformedResponseError(RuntimeError):
    """The provider response omitted the required completion message shape."""


LLM_CREDENTIAL_ATTEMPT_TIMEOUT_SECONDS = 10.0
PERSONAL_AGENT_STARTUP_TIMEOUT_SECONDS = 5.0
PERSONAL_AGENT_HEARTBEAT_TIMEOUT_SECONDS = 5.0
PERSONAL_AGENT_WATCHDOG_INTERVAL_SECONDS = 1.0
# Feature 063 US4: how often the always-on background poller checks each open
# remote Slurm job's status over SSH (read-only). Env-overridable.
REMOTE_CLUSTER_POLL_INTERVAL_SECONDS = float(os.getenv("REMOTE_CLUSTER_POLL_SECONDS", "30"))
_PERSONAL_AGENT_HOST_FRAME_TYPES = frozenset(
    {
        "agent_host_inventory",
        "agent_runtime_state",
        "agent_runtime_register",
        "agent_runtime_heartbeat",
        "agent_runtime_exit",
    }
)
_LLM_CREDENTIAL_SAVE_ACTIONS = frozenset(
    {"chrome_llm_save", "llm_config_set"}
)

_READ_ONLY_UI_ACTIONS = frozenset(
    {
        "discover_agents",
        "get_agent_permissions",
        "get_dashboard",
        "get_history",
        "get_saved_components",
        "stream_list",
    }
)
_CONVERSATION_MUTATION_ACTIONS = frozenset(
    {
        "save_component",
        "delete_saved_component",
        "combine_components",
        "condense_components",
        "component_action",
        "component_refine",
        "component_restore",
        "table_paginate",
    }
)
_CONNECTION_IDENTITY_FIELDS = frozenset(
    {
        "agent_id",
        "async_mode",
        "attachment_id",
        "chat_id",
        "component_id",
        "limit",
        "offset",
        "page",
        "probe_id",
        "schedule_id",
        "stream_id",
        "task_id",
    }
)

# The admission wrapper and the application handler execute in the same
# context.  This lets the existing UI router stay wire-compatible while the
# normal synchronous-chat path reuses the already-owned operation/fence.
_CONNECTION_OPERATION_CONTEXT: contextvars.ContextVar[dict[str, Any] | None] = (
    contextvars.ContextVar("connection_operation_context", default=None)
)
_WORKSPACE_MUTATION_LOCKS: contextvars.ContextVar[frozenset[str]] = (
    contextvars.ContextVar("workspace_mutation_locks", default=frozenset())
)
_ACTIVE_REQUEST_TEXT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "active_request_text", default=""
)


@dataclass
class _ConnectionIngressFrame:
    """One parsed, post-registration frame awaiting durable admission."""

    raw: str
    parsed: dict[str, Any]
    action: str
    surface: str | None
    chat_id: str | None
    submission_id: _uuid.UUID
    request_generation: _uuid.UUID
    normalized_digest: str
    read_only: bool
    operation_kind: str
    deadline_at_monotonic: float | None
    deadline_at_utc: datetime | None


@dataclass
class _ConnectionOperation:
    """Connection-local execution metadata for one accepted operation."""

    frame: _ConnectionIngressFrame
    owner: OperationOwner
    operation_id: _uuid.UUID
    fence: Any | None = None
    task: asyncio.Task[Any] | None = None
    predecessors: tuple[asyncio.Future[None], ...] = ()
    lane_complete: asyncio.Future[None] | None = None
    lease_lost: bool = False
    auth_principal: str | None = None
    auth_claims: dict[str, Any] = field(default_factory=dict, repr=False)
    runtime_websocket: Any | None = field(default=None, repr=False)
    committed_operation: OperationRecord | None = None
    subscribers: dict[int, tuple[Any, _ConnectionIngressFrame]] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class _VoiceDispatchContext:
    """Verified content and socket fence carried into the ordinary chat path."""

    admission: Any
    connection_generation: str
    origin: Any = field(repr=False)


@dataclass(frozen=True)
class _VoiceOperationRejection:
    """Pre-acceptance refusal that must win over generic operation success."""

    reason: str
    safe_summary: str


@dataclass(frozen=True)
class _PendingVoiceFinalization:
    """Ephemeral accepted-turn result finalized after its shared operation."""

    voice_dispatch: _VoiceDispatchContext
    user_id: str
    chat_id: str
    stage: Any


@dataclass(frozen=True)
class _VoiceOperationTerminalIntent:
    """Fixed shared-operation outcome recorded by the ordinary chat path."""

    state: OperationState
    terminal_code: str
    safe_summary: str
    retry_after_ms: int | None = None


@dataclass
class ConnectionContext:
    """Finite, tracked lifetime scope for one accepted UI socket."""

    websocket: Any
    connection_scope_id: _uuid.UUID
    registration_deadline: float
    preregistration: deque[str] = field(default_factory=deque)
    ingress: deque[_ConnectionIngressFrame] = field(default_factory=deque)
    tracked_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    operation_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    operations: dict[_uuid.UUID, _ConnectionOperation] = field(default_factory=dict)
    submission_digests: dict[_uuid.UUID, str] = field(default_factory=dict)
    terminal_emitted: set[_uuid.UUID] = field(default_factory=set)
    connection_generation: _uuid.UUID | None = None
    mutation_tail: asyncio.Future[None] | None = None
    pending_reads: set[asyncio.Future[None]] = field(default_factory=set)
    admission_task: asyncio.Task[Any] | None = None
    claim_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    registered: bool = False
    closing: bool = False

debug_mode = os.getenv("DEBUG", "false").lower() == "true"
log_level = logging.INFO if debug_mode else logging.WARNING

logging.basicConfig(level=log_level,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('Orchestrator')


class EndpointFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Filter out uvicorn access logs for "poll" endpoints and the
        # container/orchestrator health probes (they fire every few seconds).
        msg = record.getMessage()
        return not any(path in msg for path in (
            "/.well-known/agent-card.json", "/healthz", "/readyz",
        ))

# Filter uvicorn access logs if they exist
logging.getLogger("uvicorn.access").addFilter(EndpointFilter())


#: Boot relaunch (027): live server-hosted generated agents are re-Popen'd here.
#: BYO agents (058, origin ``byo_client``) are EXCLUDED — their code is the user's
#: and runs on the user's desktop host; relaunching one would put a user agent
#: process on the orchestrator host (SC-002). ``start_draft_agent`` refuses them
#: too; this filter keeps us from even asking.
LIVE_DRAFT_RELAUNCH_QUERY = (
    "SELECT id, agent_name FROM draft_agents WHERE status = 'live' "
    "AND (origin IS NULL OR origin <> 'byo_client')"
)


# Module-level singleton handle, set by Orchestrator.__init__. Used by
# external callers (e.g., feedback.cli) that need to reach into the
# running instance without going through FastAPI app.state.
_ORCH_INSTANCE = None  # type: Optional["Orchestrator"]


# Feature 008-llm-text-only-chat (FR-006a) — appended to the chat system
# prompt whenever a turn dispatches with zero usable tools. Tells the LLM
# (a) it has no tools/agents available, (b) it MUST NOT fabricate tool
# output, (c) when the user asks for an action that would require an
# agent, it should briefly state that no agents are enabled and suggest
# enabling one. The base system prompt for tool-augmented turns is
# unchanged (FR-011).
TEXT_ONLY_SYSTEM_PROMPT_ADDENDUM = """
TEXT-ONLY MODE (no agents currently available):
- You have NO tools or agents available for this turn. Do NOT emit tool calls.
- Do NOT emit any of the following tokens, in any form, as part of your reply
  text — they are tool-call markers and will be visible to the user as garbage:
    <|tool_call|>, <tool_call>, </tool_call>, <|tool_calls_section_begin|>,
    <|tool_calls_section_end|>, [TOOL_CALLS], [/TOOL_CALLS], <function_call>.
  If you would have emitted a tool call, instead write a plain-language
  sentence describing what you would have done and tell the user that no
  agents are enabled — never the raw token form.
- Do NOT fabricate tool output, pretend to have searched/fetched/created anything,
  or invent file/database/API results. If you don't actually know it, say so.
- When the user asks about recent literature, current events, news, prices, or
  anything time-sensitive: state clearly that NO live sources were retrieved and
  that your answer is general background from training data. Do NOT present
  specific dated findings, statistics, or "last N years" claims as if they were
  retrieved results, and do NOT enumerate citations you cannot source.
- If the user asks for an action that would normally require an agent (reading
  a file, searching a system, creating/modifying anything outside this chat),
  briefly note that no agents are currently enabled and suggest the user enable
  one from the Agents panel. Then offer the best help you can with text alone.
- For conversational questions, reasoning, summarization, drafting, or general
  knowledge — answer normally as a text-only chat assistant.
"""


# Chat system-prompt template. The two opaque marks are where the per-turn
# volatile sections (the file-mapping list, the live-canvas listing) are
# substituted. ``context_engineering.compose_system_prompt`` fills them in
# place by default (byte-identical to the legacy f-string), or — when
# FF_CONTEXT_ENGINEERING is on — blanks them here and appends them last so the
# stable instruction prefix stays cache-friendly.
CHAT_SYSTEM_TEMPLATE = """You are an AI orchestrator. Your goal is to simplify complex tasks for the user by intelligently using available tools.

%%ASTRAL_FILE_CONTEXT%%

AVAILABLE TOOLS: sent in the `tools` parameter.

PROCESS (Re-Act Loop):
1. **Analyze**: Break down the user's request into logical steps.
2. **Plan & Execute**:
   - If you need data, call the appropriate tool.
   - You can call multiple tools in parallel if they are independent.
   - If a step depends on previous output (e.g., "search patients" -> "graph their age"), wait for the first tool's result before calling the next.
3. **Observe**: You will receive the tool's output in the next turn.
4. **Iterate**:
   - IF the task is not complete or you need more data (e.g., now you have the patients, need to graph them), call the next tool.
   - IF you have all necessary information, provide a final answer.

CRITICAL RULES:
- **VERIFY**: Check if tool outputs actually contain the data you expect before stating it exists. If a search returns 0 results, do NOT try to graph them.
- **FINAL RESPONSE**: When you have finished all actions, provide a natural language summary of what you did and what was found.
- **CHAT IS CONCISE**: Your final chat reply must be SHORT — 2-4 plain sentences, no headings,
  no tables, no long documents. Substantial content (drafts, documents, lists, tables,
  structured data) belongs in UI components / tool outputs, not in the chat text; long text
  replies are moved to the canvas automatically.
- **VISUALIZATIONS**: If the user asks for a graph, YOU MUST call the graphing tool. Do not just describe the data.
%%ASTRAL_CANVAS_CONTEXT%%
COMPONENT UPDATE RULES:
- The user's canvas is PERSISTENT: every component listed above under COMPONENTS CURRENTLY ON CANVAS stays visible until removed, and updates replace it in place.
- When the user asks to MODIFY, UPDATE, REMOVE items from, or CHANGE existing displayed data, re-call the SAME tool that originally created it with the corrected/updated parameters. Do NOT create duplicates.
- When you author UI components directly and intend to UPDATE one listed above, set its "id" field to that component's component_id so it updates in place; omit "id" for genuinely new components.
- When the user asks for something completely NEW and unrelated, call the appropriate tool normally — the new output is added alongside the existing components.
"""


# Patterns that represent tool-call tokens leaked into text content. Some
# open-weight LLMs (Llama-style, Qwen-style, etc.) emit these even when
# instructed not to — we strip them post-hoc so the user never sees a raw
# `<|tool_call|>...` artifact in the chat. Order matters only for
# coverage; each pattern is independent.
_LEAKED_TOOL_CALL_PATTERNS = [
    # Llama-style with optional pipe variations:
    #   <|tool_call|> ... <|tool_call|>
    #   <|tool_call> ... <tool_call|>
    re.compile(r"<\|?tool_call\|?>.*?<\|?/?tool_call\|?>", re.IGNORECASE | re.DOTALL),
    # Qwen / generic XML-style tool call wrappers
    re.compile(r"<tool_call>.*?</tool_call>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<function_call>.*?</function_call>", re.IGNORECASE | re.DOTALL),
    # Llama 3 tool-calls-section markers
    re.compile(
        r"<\|tool_calls_section_begin\|>.*?<\|tool_calls_section_end\|>",
        re.IGNORECASE | re.DOTALL,
    ),
    # Mistral / generic bracket form
    re.compile(r"\[TOOL_CALLS\].*?\[/TOOL_CALLS\]", re.IGNORECASE | re.DOTALL),
    # DeepSeek DSML format — note the FULLWIDTH vertical bar (U+FF5C), not regular |.
    # Matches both the wrapper <｜DSML｜tool_calls>...</｜DSML｜tool_calls> and
    # standalone <｜DSML｜invoke ...></｜DSML｜invoke> blocks.
    re.compile(r"<｜DSML｜tool_calls>.*?</｜DSML｜tool_calls>", re.DOTALL),
    re.compile(r"<｜DSML｜invoke[^>]*>.*?</｜DSML｜invoke>", re.DOTALL),
    # 055 (D6) — XML-ish pseudo-call syntax observed live: a tool name glued
    # onto <arg_key>/<arg_value> trains (`update_component<arg_key>…`).
    # The composite (name + closed pairs) must run before the nameless pair
    # pattern so the name prefix is removed with its train; a truncated train
    # (opener never closed) strips to end-of-text — everything after it is
    # protocol syntax, not prose.
    re.compile(
        r"[A-Za-z_][A-Za-z0-9_]*\s*(?:<arg_key>.*?</arg_key>\s*(?:<arg_value>.*?</arg_value>)?\s*)+",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"<arg_(?:key|value)>.*?</arg_(?:key|value)>", re.IGNORECASE | re.DOTALL),
    re.compile(
        r"(?:[A-Za-z_][A-Za-z0-9_]*\s*)?<arg_(?:key|value)>.*$",
        re.IGNORECASE | re.DOTALL,
    ),
    # Stray dangling open tags with no close
    re.compile(r"<\|?tool_call\|?>", re.IGNORECASE),
    re.compile(r"<\|tool_calls_section_(?:begin|end)\|>", re.IGNORECASE),
    re.compile(r"\[/?TOOL_CALLS\]", re.IGNORECASE),
    re.compile(r"</?｜DSML｜[^>]*>"),
    re.compile(r"</?arg_(?:key|value)>", re.IGNORECASE),
    # 055 (D6) — NAME@true attribute trains riding alongside the pseudo-calls
    # (`NEW_PAGE@true`). Anchored on a TERMINAL boolean so addresses survive:
    # the lookahead keeps john@true.example.com intact (the "true" there is a
    # domain label, not a value).
    re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*@(?:true|false)(?![\w.@-])", re.IGNORECASE),
]


# Patterns used to extract the tool NAME from a leak match — independent of
# which wrapper pattern fired. Used by Orchestrator._diagnose_leaked_tool_calls
# to translate raw markup into a friendly user-facing alert.
_LEAK_TOOL_NAME_EXTRACTORS = [
    # DeepSeek DSML invoke tag: <｜DSML｜invoke name="tool_name">
    re.compile(r'<｜DSML｜invoke\s+name="([^"]+)"'),
    # Llama / OpenAI-style JSON tool calls embedded in leak markup
    re.compile(r'"name"\s*:\s*"([^"]+)"'),
    # Qwen <tool_call><name>tool_name</name>...
    re.compile(r"<name>\s*([A-Za-z_][A-Za-z0-9_]*)\s*</name>"),
    # Mistral [TOOL_CALLS] [{"name": "tool_name", ...}] — covered by the JSON pattern above
    # Bare function-name=... forms occasionally seen in mistral
    re.compile(r"function\s*[:=]\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)"),
]


def _tool_names_from_leak(content: str) -> List[str]:
    """Extract distinct tool names from leaked tool-call markup.

    Tries every pattern in :data:`_LEAK_TOOL_NAME_EXTRACTORS` against the
    full ``content`` blob. Returns names in first-seen order with duplicates
    removed. Returns an empty list when no recognizable tool name is found —
    in that case the caller falls back to silently stripping the markup.
    """
    if not content:
        return []
    seen: List[str] = []
    seen_set: set = set()
    for pat in _LEAK_TOOL_NAME_EXTRACTORS:
        for match in pat.finditer(content):
            name = match.group(1)
            if name and name not in seen_set:
                seen_set.add(name)
                seen.append(name)
    return seen


# Diagnostic statuses returned by Orchestrator._diagnose_disabled_tool.
# Defined at module scope so tests can import them by name.
class ToolDiagnosticStatus(str, Enum):
    ENABLED = "enabled"
    DISABLED_IN_PICKER = "disabled_in_picker"
    AGENT_DISABLED_BY_USER = "agent_disabled_by_user"
    PERMISSION_DENIED = "permission_denied"
    SECURITY_BLOCKED = "security_blocked"
    UNKNOWN_TOOL = "unknown_tool"


class ToolDiagnostic(NamedTuple):
    status: ToolDiagnosticStatus
    agent_id: Optional[str]
    agent_display_name: Optional[str]
    reason: Optional[str]


def _strip_toolcall_leakage(content: str) -> str:
    """Remove leaked tool-call markup from model-authored text (055 D6).

    Shared by the final-text sanitizer, the chat narrative, the canvas
    doc-card promotion, and the round-summary path. Returns the stripped
    text (possibly empty) — each caller owns its honest fallback.
    """
    cleaned = content or ""
    for pat in _LEAKED_TOOL_CALL_PATTERNS:
        cleaned = pat.sub("", cleaned)
    return cleaned.strip()


# 055 (D6): honest fallback when stripping empties a model response — the
# user gets a short actionable line instead of an empty bubble/card; the raw
# payload goes to the diagnostic log only, never the render surface.
_LEAK_FALLBACK_TEXT = (
    "The AI model's response contained only tool-call markup, so there is "
    "nothing to show for this turn. Please try again."
)


def _log_stripped_empty(surface: str, chat_id: Optional[str], raw: str) -> None:
    logger.warning(
        "toolcall_leak.stripped_empty surface=%s chat=%s raw=%r",
        surface, chat_id, raw[:500],
    )


#: 055 US4 (wire-contract §6): the server-owned provenance vocabulary.
_PROVENANCE_KINDS = ("grounded", "estimated", "generated")


def _derive_provenance(comp) -> str:
    """Reuses the web footer's subtree derivation (renderer._subtree_tool_source)
    so server stamp and footer always classify identically: a subtree tracing
    to a tool result is "grounded", anything else "generated". "estimated" is
    never derivable — only server code may assign it (a refine that did not
    re-run the source tool, research D10) via ``_stamp_provenance(kind=...)``.
    """
    from webrender.renderer import _subtree_tool_source
    return "grounded" if _subtree_tool_source(comp) else "generated"


def _stamp_provenance(comp, kind=None) -> None:
    """055 US4 (FR-026): stamp ``provenance`` on a component dict, ALWAYS
    overwriting any agent/model-supplied value — trust cannot be
    self-upgraded. ``kind`` is a server-side override; anything outside the
    vocabulary falls back to derivation. With FF_COMPONENT_REFINE off the
    field is STRIPPED, never merely left alone: 055-era clients render trust
    badges from this field, so letting an agent-supplied value ride through
    with the kill switch off would let agents mint their own badges (FR-026
    has no flag carve-out).
    """
    if not isinstance(comp, dict):
        return
    if not flags.is_enabled("component_refine"):
        comp.pop("provenance", None)
        return
    comp["provenance"] = kind if kind in _PROVENANCE_KINDS else _derive_provenance(comp)


def _stamp_canvas_provenance(components) -> None:
    """Stamp a materialized canvas — the last stop before delivery, and the
    only place designer garnish exists as component dicts. Garnish (``dg_``
    ids, rebuilt from the layout JSON on every materialization) can never
    self-assign trust, so it is re-derived unconditionally — a garnish
    container wrapping tool-sourced refs correctly reads grounded; persisted
    components keep their server stamp, and legacy (pre-055) rows without one
    are derived in place.
    """
    if not flags.is_enabled("component_refine"):
        for comp in components or []:
            if isinstance(comp, dict):
                comp.pop("provenance", None)
        return
    from orchestrator.ui_designer import GARNISH_ID_PREFIX
    for comp in components or []:
        if not isinstance(comp, dict):
            continue
        ident = str(comp.get("id") or comp.get("component_id") or "")
        if ident.startswith(GARNISH_ID_PREFIX) or comp.get("provenance") not in _PROVENANCE_KINDS:
            _stamp_provenance(comp)


def _tag_source(comp, agent_id, tool_name, tool_params=None, correlation_id=None):
    """Recursively tag a component dict and all nested children with source
    metadata (055 US2: hoisted from handle_chat_message so the stream
    persist-on-terminal path stamps identical provenance).

    `tool_params` is only tagged on the top-level node — the auto-subscribe
    path reads it there to replay the same arguments on `stream_subscribe`.

    Feature 004: `correlation_id` is the audit-log id of the originating
    tool dispatch. When present, every component (including nested children)
    carries it so the frontend can scope user feedback to the originating
    dispatch.

    055 US4: every tagged node also gets its ``provenance`` field stamped
    from the just-written source attribution (agent-supplied values are
    always overwritten — FR-026).
    """
    if not isinstance(comp, dict):
        return
    comp["_source_agent"] = agent_id
    comp["_source_tool"] = tool_name
    if tool_params is not None:
        comp["_source_params"] = tool_params
    if correlation_id is not None:
        comp["_source_correlation_id"] = correlation_id
    _stamp_provenance(comp)
    for key in ("content", "children"):
        nested = comp.get(key)
        if isinstance(nested, list):
            for child in nested:
                _tag_source(child, agent_id, tool_name, correlation_id=correlation_id)


def _sanitize_text_response(content: str) -> str:
    """Strip leaked tool-call tokens from a text response.

    Some LLMs (especially open-weight Llama-style models) emit their
    tool-call tokenization as plain text when they're asked to invoke a
    tool but no tools are available — leaving the user staring at raw
    `<|tool_call|>...<tool_call|>` markup. The system prompt addendum
    asks the LLM not to do this, but we cannot rely on prompt
    compliance, so we strip the patterns here as a defensive layer.

    If the entire response was a leaked tool call (nothing useful left
    after stripping), returns a friendly fallback so the user gets an
    actionable message instead of an empty bubble.
    """
    if not content:
        return content
    cleaned = _strip_toolcall_leakage(content)
    if not cleaned:
        return (
            "No agents are currently enabled, so I can't run that for you. "
            "Open the Tools & Agents picker (wrench icon next to the send "
            "button) and re-enable an agent, then try again."
        )
    return cleaned


# Feature 029 — catalog change handling for historical components
# (specs/029-agents-adaptive-ui-ci/baseline.md). Six agents are retired
# outright; three merged into ml-services-1. Sources remap so refresh /
# pagination on pre-merge components keeps working; retired sources get an
# explicit retirement message instead of a dispatch crash. Module-level (not
# class attributes) so unbound-method test fakes need no extra wiring.
RETIRED_AGENT_IDS = frozenset({
    "email_tracker", "email-tracker-1", "grant_budgets", "grant-budgets-1",
    "grants", "grants-1", "linkedin", "linkedin-1",
    "nefarious", "nefarious-1", "nocodb", "nocodb-1",
    # Feature 040: etf_tracker_1 retired (agent removed). Both the hyphenated
    # agent id and the legacy underscore directory-name form route through the
    # runtime retirement handling so old transcripts degrade gracefully.
    "etf_tracker_1", "etf-tracker-1-1",
})
_MERGED_AGENT_REMAP = {
    "classify": "ml-services-1", "classify-1": "ml-services-1",
    "forecaster": "ml-services-1", "forecaster-1": "ml-services-1",
    "llm_factory": "ml-services-1", "llm-factory-1": "ml-services-1",
    # Feature 063: the split read-only + control agents merged into the single
    # remote-compute-1. Verb names are identical across the merge, so old
    # transcript component sources reroute with no tool-name rewrite (no prefix
    # entry needed) — a refresh transparently re-runs on the unified agent.
    "remote-observe-1": "remote-compute-1", "remote-control-1": "remote-compute-1",
}
_MERGED_TOOL_PREFIX = {
    "classify": "classify_", "classify-1": "classify_",
    "forecaster": "forecaster_", "forecaster-1": "forecaster_",
}
_MERGED_COLLIDING_VERBS = frozenset({
    "submit_dataset", "start_training_job", "get_job_status",
    "get_results", "delete_dataset",
})


_ASSET_VERSION_CACHE: Dict[str, str] = {}
_ASSET_VERSION_MAP_CACHE: Dict[str, Dict[str, str]] = {}
_ASSET_TOKEN_RE = re.compile(r"%%ASTRAL_V:([^%]+)%%")

# Feature 052 (FR-015): the chat loop opts its route-LLM call into narrative
# streaming through this context (value = the turn's chat id) instead of new
# _call_llm arguments, so the many tests and callers that stub _call_llm with
# the historical signature keep working unchanged.
_NARRATIVE_STREAM_CHAT: contextvars.ContextVar = contextvars.ContextVar(
    "narrative_stream_chat", default=None)


def _static_asset_version(static_dir: str) -> str:
    """Return a short combined content hash of ``client.js`` + ``astral.css``.

    Legacy feature-040 helper kept for its existing callers/tests; the shell
    now versions every asset individually via :func:`_static_version_map`.
    Memoized per directory (assets are baked, immutable per process).
    """
    cached = _ASSET_VERSION_CACHE.get(static_dir)
    if cached:
        return cached
    import hashlib
    import os as _o
    h = hashlib.sha1()
    for name in ("client.js", "astral.css"):
        try:
            with open(_o.path.join(static_dir, name), "rb") as fh:
                h.update(fh.read())
        except OSError:
            pass
    ver = h.hexdigest()[:12] or "dev"
    _ASSET_VERSION_CACHE[static_dir] = ver
    return ver


def _static_version_map(static_dir: str) -> Dict[str, str]:
    """Per-file content-hash version map over the whole static tree.

    Maps each asset's path relative to ``static_dir`` (forward slashes, e.g.
    ``fonts/inter-latin.woff2``) to ``sha1(file bytes)[:12]``. Built once per
    directory per process (feature 052, contracts/static-asset-caching.md):
    a deploy is a new process, so new bytes always get new URLs.
    """
    cached = _ASSET_VERSION_MAP_CACHE.get(static_dir)
    if cached is not None:
        return cached
    import hashlib
    import os as _o
    versions: Dict[str, str] = {}
    with perf_span("static.version_map"):
        for root, _dirs, files in _o.walk(static_dir):
            for name in files:
                full = _o.path.join(root, name)
                rel = _o.path.relpath(full, static_dir).replace(_o.sep, "/")
                try:
                    h = hashlib.sha1()
                    with open(full, "rb") as fh:
                        for block in iter(lambda: fh.read(1 << 20), b""):
                            h.update(block)
                    versions[rel] = h.hexdigest()[:12]
                except OSError:
                    continue
    _ASSET_VERSION_MAP_CACHE[static_dir] = versions
    return versions


def _apply_asset_versions(shell: str, static_dir: str) -> str:
    """Substitute every ``%%ASTRAL_V:<relpath>%%`` shell token with that
    file's current content hash (``dev`` for an unknown path so the URL is
    still well-formed and served under the no-cache flow)."""
    versions = _static_version_map(static_dir)
    return _ASSET_TOKEN_RE.sub(lambda m: versions.get(m.group(1), "dev"), shell)


class _NoCacheStaticFiles(StaticFiles):
    """Version-aware static files: immutable when the URL proves freshness.

    A request whose ``?v=`` matches the file's current content hash is
    immutable by construction (changed bytes get a new URL from the shell),
    so it gets a year-long ``immutable`` Cache-Control. Every other request
    keeps the feature-040 ``no-cache`` + ETag/Last-Modified 304 flow — this
    includes the deliberately unversioned CSS ``@font-face`` URLs.
    """

    async def get_response(self, path, scope):
        """Serve the asset with a version-dependent Cache-Control header."""
        response = await super().get_response(path, scope)
        cache_control = "no-cache"
        try:
            from urllib.parse import parse_qs
            query = (scope.get("query_string") or b"").decode("latin-1")
            requested = (parse_qs(query).get("v") or [None])[0]
            if requested:
                rel = str(path).replace("\\", "/").lstrip("/")
                current = _static_version_map(str(self.directory)).get(rel)
                if current and requested == current:
                    cache_control = "public, max-age=31536000, immutable"
        except Exception:
            cache_control = "no-cache"
        try:
            response.headers["Cache-Control"] = cache_control
        except Exception:
            pass
        return response

# 030: per-tool dispatch ceilings for long-running verbs (everything else
# keeps the 30 s default). research_brief legitimately performs one search
# plus several 15 s-bounded page fetches — the default ceiling guaranteed
# "Tool call timed out" (live incident).
TOOL_TIMEOUT_OVERRIDES = {
    "research_brief": 150.0,
    "fetch_page": 45.0,
    "summarize_url": 60.0,
    "compare_documents": 60.0,
}


def remap_merged_source(agent_id: str, tool_name: str):
    """Map a pre-merge (agent, tool) provenance onto the ml-services-1 agent.

    The five verbs classify and forecaster shared pre-merge carry a service
    prefix in the consolidated registry; everything else keeps its name.
    Unrelated agents pass through untouched.
    """
    new_agent = _MERGED_AGENT_REMAP.get(agent_id)
    if not new_agent:
        return agent_id, tool_name
    prefix = _MERGED_TOOL_PREFIX.get(agent_id, "")
    if prefix and tool_name in _MERGED_COLLIDING_VERBS:
        tool_name = prefix + tool_name
    return new_agent, tool_name


def _is_native_device(profile) -> bool:
    """Windows/Android/iOS/macOS/watch — surfaces that render structured
    components with their own layout engine (not the designer's web HTML)."""
    from rote.capabilities import DeviceType
    return profile is not None and profile.device_type in (
        DeviceType.WINDOWS, DeviceType.ANDROID,
        DeviceType.IOS, DeviceType.MACOS, DeviceType.WATCH)


def _native_canvas_components(components) -> List[Dict[str, Any]]:
    """055 US3 (wire-contract §5): the materialized canvas for NATIVE delivery
    excludes ``doc_`` narrative cards and model "Reasoning" collapsibles —
    their reducers divert those to the chat rail, so shipping them in a canvas
    frame would have them silently dropped client-side. Matchers mirror the
    clients' (AppViewModel.kt isDocCard/isReasoning and the Swift twin)."""
    out = []
    for c in components or []:
        if not isinstance(c, dict):
            continue
        # The author id is "doc_…"; the workspace persists it under the
        # namespaced "au_doc_…" identity — match both.
        if (str(c.get("id") or "").startswith("doc_")
                or str(c.get("component_id") or "").startswith(("doc_", "au_doc_"))):
            continue
        if (str(c.get("type") or "").lower() == "collapsible"
                and str(c.get("title") or "").strip().lower() == "reasoning"):
            continue
        out.append(c)
    return out


class PreparedDispatch(NamedTuple):
    """Outcome of ``Orchestrator._authorize_and_prepare`` when every gate
    allows the call (056 US3). Carries the fully prepared arguments (path
    mapping, credential/LLM-credential injection, policy rewrites, delegation
    token, cap job id) so the caller only has to dispatch and deliver."""
    args: Dict[str, Any]
    stream_params: Dict[str, Any]
    cap_job_id: Optional[str]
    delegation_token: Optional[str]
    # 056 US1: on a chained hop, the correlation id shared by the hop's
    # delegation.hop.mint/.enforce records — threaded into ToolDispatchAudit
    # so the hop's tool.start/end pair shares it too (SC-003 reconstruction).
    hop_correlation_id: Optional[str] = None


class GateRefusal(NamedTuple):
    """Outcome of ``Orchestrator._authorize_and_prepare`` when a gate denies
    the call (056 US3). ``response`` is the refusal the dispatch path must
    return; ``render_components`` are the alert dicts the caller renders (may
    differ from ``response.ui_components`` — e.g. the no-agent and
    delegation-required refusals render an alert the response doesn't carry;
    hook blocks render nothing). ``render_target`` preserves each gate's
    historical ``send_ui_render`` target (None = default)."""
    response: MCPResponse
    render_components: Optional[List[Dict[str, Any]]] = None
    render_target: Optional[str] = None
    # 056: True when the refusal already emitted its own delegation hop record
    # (the child-mint/enforce refusals do), so the wrapper does not double-audit.
    hop_audited: bool = False


class Orchestrator:
    def __init__(self):
        # 020-async-queries: background task manager for async chat processing
        from orchestrator.async_tasks import BackgroundTaskManager
        self.async_task_manager = BackgroundTaskManager()
        self.agents: Dict[str, websockets.WebSocketServerProtocol] = {}
        # Feature 040 (US1): bundled first-party agents running IN-PROCESS
        # (agent_id -> live BaseA2AAgent instance). Dispatch selects the
        # in-process path by a positive membership check here; external A2A and
        # draft-subprocess agents are unaffected.
        self.local_agents: Dict[str, Any] = {}
        self.ui_clients: List[websockets.WebSocketServerProtocol] = []
        self.ui_sessions: Dict[websockets.WebSocketServerProtocol, Dict] = {}
        self.agent_cards: Dict[str, AgentCard] = {}
        self.agent_capabilities: Dict[str, List[Dict]] = {}
        self.pending_requests: Dict[str, asyncio.Future] = {}
        # request_id -> the agent id the request was DISPATCHED to. Response
        # correlation is keyed on request_id alone, which is safe only while every
        # responder is trusted; untrusted BYO agents now share this router, so the
        # dispatch target is recorded and a response arriving from a DIFFERENT
        # agent's socket is dropped (defense in depth — uuid4 request ids make it
        # unguessable today, so this closes the seam rather than a live hole).
        self._pending_request_agent: Dict[str, str] = {}
        self.pending_ui_sockets: Dict[str, Any] = {}  # request_id -> UI websocket (for progress forwarding)
        # 015-external-ai-agents: per-(user, agent) concurrency cap for long-running tools (FR-026).
        self.concurrency_cap = ConcurrencyCap(max_per_user_agent=3)
        # Maps cap_job_id -> (user_id, agent_id) so terminal ToolProgress can release the right slot.
        self._pending_cap_entries: Dict[str, tuple] = {}
        # 056 US3 (FR-019): a chained hop's long-running work ALSO charges the
        # initiating agent's (user, agent) slot; this maps cap_job_id → that
        # second slot so every release site frees both sides.
        self._hop_cap_entries: Dict[str, tuple] = {}
        # 056 US1: orchestrator-side record of every in-flight agent dispatch
        # (request_id → user/chat/ui-socket/agent/decoded parent token). A
        # mediated hop request resolves its authority against THIS record —
        # never against agent-supplied identity — closing the confused-deputy
        # seam. Entries live exactly as long as their dispatch.
        self._dispatch_context: Dict[str, Dict[str, Any]] = {}
        # Strong refs to in-flight hop-mediation tasks (asyncio keeps weak refs).
        self._background_hop_tasks: set = set()
        # 056 US1/US4 (FR-021): per-turn global chain budgets keyed by chat id
        # (reset at each turn start; lazily created on first hop).
        self._chain_budgets: Dict[str, Any] = {}
        # 058 (BYO agents): user-agent tunnel sockets keyed by (owner_sub,
        # agent_id). A user's desktop-hosted agent tunnels its frames over the
        # owner's authenticated UI socket; this maps each tunneled agent to its
        # TunnelSocket adapter so dispatch routes back to the client. Cleared on
        # UI disconnect (honest-offline).
        self._tunnel_sockets: Dict[tuple, Any] = {}
        # 058 (per-owner ingress bound, FR-017/SC-008): fixed-window frame-rate
        # counter per owner sub on the agent tunnel, so a flooding/runaway user
        # agent degrades only its own owner. {owner_sub: [window_start, count]}.
        self._tunnel_ingress: Dict[str, list] = {}
        # 058 (code delivery, FR-004): the UI sockets that declared themselves
        # DESKTOP HOSTS at register_ui — the only sockets a generated agent
        # bundle is ever pushed to. {id(websocket): host_session_id}. A browser
        # tab never appears here, so authoring in a tab with no desktop client
        # running reports 'no_host' instead of pushing the user's generated code
        # into the browser and calling it delivered.
        self._agent_host_sockets: Dict[int, str] = {}
        # Maps cap_job_id -> {user_id, agent_id, chat_id, tool_name} for long-running
        # jobs, so a job's progress + terminal result can be routed to (and
        # persisted in) the originating CHAT — not a single ephemeral socket —
        # which keeps auto-progress working across refresh / device changes.
        self._job_context: Dict[str, Dict[str, Any]] = {}
        self.cancelled_sessions: Dict[str, bool] = {}  # websocket id -> cancelled flag
        self._chat_locks: Dict[int, asyncio.Lock] = {}  # per-websocket lock for chat serialization
        self._registered_events: Dict[int, asyncio.Event] = {}  # gate non-register messages until auth completes
        # Feature 065: only non-secret signed scope is retained for an active
        # UI socket. The bearer is sent once and never stored server-side.
        self._voice_binding_issuer: VoiceControlBindingIssuer | None = None
        self._voice_control_bindings: Dict[int, VoiceControlClaims] = {}
        self._voice_device_bindings: Dict[tuple[str, str], int] = {}
        self._voice_device_kinds: Dict[tuple[str, str], str] = {}
        self._voice_composer_revisions: Dict[int, int] = {}
        self._voice_composer_tasks: Dict[int, asyncio.Task[Any]] = {}
        # Feature 060: every accepted UI socket owns one finite connection
        # scope.  The capacity signal is loop-local and lazily created.
        self._connection_contexts: Dict[int, ConnectionContext] = {}
        # Reconnectable USER-owned operations outlive any one socket.  This
        # process-local registry is a single-flight execution reservation and
        # a strong task reference; durable reconciliation remains PostgreSQL.
        self._reconnectable_operations: Dict[
            _uuid.UUID, _ConnectionOperation
        ] = {}
        self._reconnectable_operation_tasks: set[asyncio.Task[Any]] = set()
        self._interactive_capacity_event: asyncio.Event | None = None
        self._interactive_capacity_revision = 0

        # Live streaming subscriptions (existing POLLING path — kept for tools
        # that declare streaming_kind == "poll")
        self._stream_tasks: Dict[int, Dict[str, asyncio.Task]] = {}   # ws_id -> {tool_name -> Task}
        self._stream_subs: Dict[int, Dict[str, Dict]] = {}            # ws_id -> {tool_name -> config}
        self._streamable_tools: Dict[str, Dict] = {}                  # tool_name -> {agent_id, default_interval, min_interval, max_interval, kind}
        self._MAX_STREAM_SUBSCRIPTIONS = 10

        # 001-tool-stream-ui: PUSH streaming via StreamManager. Constructed
        # below after self.rote is initialized; the manager wires into
        # _safe_send and ui_sessions for per-subscriber authorization.
        self.stream_manager: Optional[StreamManager] = None  # populated post-init

        # 001-tool-stream-ui: per-ws "currently active chat" tracker. Used by
        # pause_chat / resume on load_chat transitions so the stream manager
        # knows which chat to pause for THIS websocket (each tab has its own
        # active chat — pausing/resuming one tab must not affect others).
        # Keyed by id(websocket).
        self._ws_active_chat: Dict[int, str] = {}
        # Feature 060: the exact committed/transient generation currently
        # selected by each socket. Values are server-bound only after owner
        # validation (hydration) or a fenced turn stage (commit).
        self._conversation_scopes: Dict[int, Dict[str, Any]] = {}

        # Feature 028 — per-socket read-only timeline mode (mutating
        # component actions are refused server-side while set) and per-chat
        # serialization locks for deterministic component-action ordering.
        self._ws_timeline_mode: Dict[int, bool] = {}
        self._workspace_locks: Dict[str, asyncio.Lock] = {}

        # Sockets currently showing the server-driven welcome canvas (example
        # queries pushed after register_ui). The first chat message blanks the
        # canvas so flat ui_upsert appends never land under the examples.
        self._ws_welcome: Dict[int, bool] = {}

        # Feature 014 — per-active-turn step recorders, keyed by id(websocket).
        # Created at the start of handle_chat_message and torn down at the end
        # of _serialized_chat. The cancel_task handler reads this map to invoke
        # cancel_all_in_flight() (FR-020/021).
        self._chat_recorders: Dict[int, Any] = {}

        # A2A external agent connections (JSON-RPC transport)
        self.a2a_clients: Dict[str, Any] = {}  # agent_id -> A2A client
        self.a2a_agent_cards: Dict[str, Any] = {}  # agent_id -> official A2A AgentCard
        self.agent_urls: Dict[str, str] = {}  # agent_id -> base URL (for peer registry)

        # LLM credentials (feature 054-byo-llm-setup; supersedes 006's
        # operator-default model)
        # ----------------------------------------------------------------
        # There is NO deployment-supplied default LLM credential. Every
        # user brings their own provider via the mandatory first-run
        # setup dialog; the record persists server-side (user_llm_config,
        # API key Fernet-encrypted) and resolves by user_id. Server-
        # initiated/system work resolves the admin-managed
        # system_llm_config record instead — never a user's, and never
        # the other way around (FR-019). The store itself is created
        # after HistoryManager below (it needs the DB handle).
        from llm_config import (
            build_llm_client,
            CredentialSource,
            LLMUnavailable,
            ResolvedConfig,
        )
        from llm_config.audit_events import (
            record_llm_call,
            record_llm_unconfigured,
        )
        from llm_config.log_scrub import install_redaction_filter
        # Cache the imports as instance attributes so the hot _call_llm
        # path doesn't re-import on every call.
        self._build_llm_client = build_llm_client
        self._CredentialSource = CredentialSource
        self._LLMUnavailable = LLMUnavailable
        self._ResolvedConfig = ResolvedConfig
        self._record_llm_call = record_llm_call
        self._record_llm_unconfigured = record_llm_unconfigured
        # Root-logger API-key redaction (spec FR-006). The filter existed
        # since 006 but was never installed; 054 wires it at boot.
        install_redaction_filter()

        # Feature 054 kill switch: governs only the register-time mandatory
        # dialog push. The credential requirement itself is structural (no
        # default exists), so gate REFUSALS stay in force with the flag off.
        self._ff_llm_first_run = os.getenv(
            "FF_LLM_FIRST_RUN", "true").lower() in ("true", "1", "yes")

        # Default reasoning-effort knob threaded through _call_llm. Unset →
        # nothing is sent (zero behavior change on endpoints that predate
        # reasoning models). Callers may override per-call; this is only the
        # global default.
        self.llm_reasoning_effort = self._valid_reasoning_effort(
            os.getenv("LLM_REASONING_EFFORT")
        )
        # Per-(base_url, model) capability cache of optional request params the
        # endpoint rejected, so we probe once and then stop sending them.
        # {(base_url, model): {"response_format", …}}.
        self._llm_unsupported_params: Dict[tuple, set] = {}

        # When datamarking is engaged, also surgically remove well-known
        # instruction-override spans from untrusted tool output (optional
        # span-level removal). Off by default — the default defense is
        # delimiting only, which never mutates content.
        self._datamark_sanitize_spans = os.getenv(
            "DATAMARK_SANITIZE_SPANS", "false"
        ).lower() in ("true", "1", "yes")

        # History Manager
        backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        data_dir = os.path.join(backend_dir, 'data')
        self.history = HistoryManager(data_dir=data_dir)
        # Feature 065: voice is an included server-owned capability, but its
        # isolated media/control plane fails closed without affecting typed
        # chat. Speech endpoint credentials are never present in this process.
        self.voice_services = None
        self.voice_runtime = None
        self.voice_worker_pool = None
        self.voice_worker_endpoint = None
        try:
            from orchestrator.voice_bootstrap import build_voice_services

            self.voice_services = build_voice_services(self.history.db)
            self.voice_runtime = self.voice_services.runtime
            self.voice_worker_pool = self.voice_services.worker_pool
            self.voice_services.bind_terminal_turn_notifier(
                self._notify_reconciled_voice_terminal_turn
            )
        except Exception as exc:
            logger.warning(
                "conversational_voice_unavailable",
                extra={"reason": getattr(exc, "code", type(exc).__name__)},
            )

        # Feature 060: one PostgreSQL operation/admission authority shared by
        # every compatibility manager.  The migration has already established
        # all six class rows; load their effective persisted values so operator
        # tuning is preserved across restart instead of reapplying defaults.
        operation_retention_seconds = int(
            os.getenv("OPERATION_RETENTION_SECONDS", "86400")
        )
        if operation_retention_seconds <= 0:
            raise ValueError("OPERATION_RETENTION_SECONDS must be positive")
        self.work_admission = WorkAdmissionCoordinator.from_database(
            database=self.history.db,
            operation_retention=timedelta(seconds=operation_retention_seconds),
        )
        # Feature 060: PostgreSQL is the sole authority for personal-agent
        # host selection, immutable revisions, runtime generations, and calls.
        # The process-local maps below are wake-up/routing projections only;
        # every transition validates the durable fence before using them.
        from orchestrator.agent_generator import (
            BYO_RUNTIME_CONTRACT_VERSION,
            BYO_RUNTIME_LOCK_SHA256,
        )
        from orchestrator.agent_lifecycle import PostgresPersonalAgentRevisionStore
        from orchestrator.artifact_publication import ImmutableAgentArtifactStore
        from orchestrator.user_agents import (
            PersonalAgentRuntimeRepository,
            RuntimeCompatibilityPolicy,
        )

        personal_agent_policy = RuntimeCompatibilityPolicy(
            runtime_contract_version=BYO_RUNTIME_CONTRACT_VERSION,
            runtime_lock_sha256=BYO_RUNTIME_LOCK_SHA256,
        )
        self.personal_agent_runtime = PersonalAgentRuntimeRepository(
            self.history.db,
            compatibility_policy=personal_agent_policy,
            operation_repository=self.work_admission.repository,
            operation_retention=timedelta(seconds=operation_retention_seconds),
        )
        self.personal_agent_revisions = PostgresPersonalAgentRevisionStore(
            self.personal_agent_runtime
        )
        self.personal_agent_artifacts = ImmutableAgentArtifactStore()
        self.personal_agent_capabilities = CandidateCapabilityMap()
        self._personal_agent_host_sessions: Dict[int, Any] = {}
        self._personal_agent_session_sockets: Dict[str, Any] = {}
        self._personal_agent_ready_waiters: Dict[str, asyncio.Future[str]] = {}
        self._personal_agent_request_waiters: Dict[str, asyncio.Future[Any]] = {}
        self._personal_agent_runtime_sockets: Dict[str, Any] = {}
        self._personal_agent_candidate_cards: Dict[str, AgentCard] = {}
        self._personal_agent_activation_locks: Dict[
            tuple[str, str, str], asyncio.Lock
        ] = {}
        self._personal_agent_watchdog_task: asyncio.Task[Any] | None = None
        # Feature 063 US4: the always-on remote-Slurm-job status poller (launched
        # in start() only when FF_REMOTE_COMPUTE is on; cancelled on shutdown).
        self._remote_job_poll_task: asyncio.Task[Any] | None = None
        self.runtime_observability = RuntimeObservability(
            retention_seconds=operation_retention_seconds,
            deployment_instance=os.getenv(
                "RUNTIME_METRICS_INSTANCE",
                "astraldeep",
            ),
        )
        self.task_manager = TaskManager(self.work_admission)
        self.conversation_commits = ConversationCommitRepository(
            self.history.db,
            operation_coordinator=self.work_admission,
        )

        # 055 bg-continuity: durable task records + a completion fan that
        # reaches every socket of the user (the originator may be gone). The
        # manager itself is constructed above, before the DB exists.
        self.async_task_manager.bind(
            coordinator=self.work_admission,
            db=self.history.db,
            on_complete=self._fan_task_completed,
            observability=self.runtime_observability,
        )

        # Feature 054: persisted per-user + system LLM configuration store
        # (user_llm_config / system_llm_config tables; Fernet under
        # CREDENTIAL_ENCRYPTION_KEY with the shared dev key-file fallback).
        from llm_config.user_store import UserLLMConfigStore
        self._llm_store = UserLLMConfigStore(self.history.db, data_dir=data_dir)

        # Feature 028 — per-chat persistent workspace (identity, upserts,
        # snapshots/timeline). Owns the saved_components store.
        from orchestrator.workspace import WorkspaceManager
        self.workspace = WorkspaceManager(self.history)

        # File-tool DB wiring (feature 002-file-uploads). Lets the
        # in-process tool functions resolve attachments without going
        # through HTTP.
        try:
            from agents.general.file_tools import register_database as _register_file_tools_db
            _register_file_tools_db(self.history.db)
        except Exception as _exc:  # pragma: no cover - non-fatal
            logger.warning(f"file_tools DB wiring skipped: {_exc}")

        # Tool Permission Manager (RFC 8693 delegation) — backed by same PostgreSQL DB
        self.tool_permissions = ToolPermissionManager(db=self.history.db, data_dir=data_dir)

        # Per-user credential storage (encrypted API keys for agents)
        self.credential_manager = CredentialManager(db=self.history.db, data_dir=data_dir)

        # Delegation Service (RFC 8693 token exchange)
        self.delegation = DelegationService()

        # Tool Security Analyzer — proactive security review of agent tools
        self.security_analyzer = ToolSecurityAnalyzer()
        self.security_flags: Dict[str, Dict[str, Any]] = {}  # agent_id -> {tool_name: flag_dict}

        # LLM Token Usage Tracking — per-conversation accumulation
        self.token_usage: Dict[str, Dict[str, int]] = {}  # chat_id -> {prompt_tokens, completion_tokens, total_tokens}

        # ROTE — Response Output Translation Engine
        self.rote = ROTE()

        # 001-tool-stream-ui: instantiate the push-streaming manager now that
        # ROTE exists. Wires _safe_send for per-subscriber delivery,
        # ui_sessions for the per-chunk authorization invariant
        # (data-model.md §8), and the streaming agent dispatcher / canceller
        # methods defined below for routing MCPRequest with _stream=True.
        self.stream_manager = StreamManager(
            rote=self.rote,
            send_to_ws=self._safe_send,
            get_user_session=lambda ws: self.ui_sessions.get(ws),
            agent_dispatcher=self._dispatch_stream_request,
            agent_canceller=self._cancel_stream_request,
            validate_chat_ownership=self._validate_chat_ownership_for_stream,
        )
        # 055 US2 (FR-011): out-of-band terminals (retry exhaustion, dormant
        # TTL eviction, resume-dispatch failure) reach the persist path via
        # this hook; the in-band wrapper in handle_agent_message covers frames
        # that arrive as agent messages. persist_done keeps them idempotent.
        self.stream_manager.terminal_hook = self._persist_stream_terminal

        # Hook/Event System — extensible lifecycle events
        self.hooks = HookManager()

        # Audit log (003-agent-audit-log) — repository, recorder, publisher
        from audit.repository import AuditRepository
        from audit.recorder import Recorder, set_recorder
        from audit.ws_publisher import make_publish_callable
        self.audit_repo = AuditRepository(self.history.db)
        self.audit_recorder = Recorder(self.audit_repo)
        self.audit_recorder.set_publisher(make_publish_callable(self))
        set_recorder(self.audit_recorder)

        # Feature 004 — component feedback & tool-improvement loop
        from feedback.repository import FeedbackRepository
        from feedback.recorder import Recorder as FeedbackRecorder
        self.feedback_repo = FeedbackRepository(self.history.db)
        self.feedback_recorder = FeedbackRecorder(self.feedback_repo)

        # Feature 005 — tool tips and getting started tutorial
        from onboarding.repository import OnboardingRepository
        from onboarding.seed import seed_tutorial_steps
        self.onboarding_repo = OnboardingRepository(self.history.db)
        try:
            seed_tutorial_steps(self.history.db)
        except Exception as exc:  # pragma: no cover — never block startup
            logger.warning(f"Tutorial seed loader failed (non-fatal): {exc}")
        # Feature 025 — per-user personalization (profile, personality, memory)
        from personalization.service import PersonalizationService
        self.personalization_service = PersonalizationService(self.history.db)
        # Publish self as the module-level singleton so the feedback CLI
        # and the pre-pass entrypoint can find the synthesizer without
        # going through FastAPI app.state.
        global _ORCH_INSTANCE
        _ORCH_INSTANCE = self

        # Agent Lifecycle Manager — handles user-created draft agents
        from orchestrator.agent_lifecycle import AgentLifecycleManager
        self.lifecycle_manager = AgentLifecycleManager(db=self.history.db, orchestrator=self)
        self.lifecycle_manager.personal_agent_runtime = self.personal_agent_runtime
        self.lifecycle_manager.personal_agent_revisions = self.personal_agent_revisions

        # Knowledge Synthesis ("Dreamer") — learns from tool interactions
        if flags.is_enabled("knowledge_synthesis"):
            from orchestrator.knowledge_synthesis import (
                InteractionCollector, KnowledgeSynthesizer, KnowledgeIndex,
            )
            knowledge_dir = os.path.join(
                os.path.abspath(os.path.join(os.path.dirname(__file__), '..')),
                "knowledge",
            )
            self.knowledge_index = KnowledgeIndex(knowledge_dir)
            self._interaction_collector = InteractionCollector(db=self.history.db)
            self._knowledge_synthesizer = KnowledgeSynthesizer(
                db=self.history.db,
                knowledge_dir=knowledge_dir,
                knowledge_index=self.knowledge_index,
                # Feature 054: cross-user system flow — runs on the admin-
                # managed system credential, re-resolved per cycle.
                config_resolver=self._llm_store.get_system_sync,
            )
            self.hooks.register(HookEvent.POST_TOOL_USE, self._interaction_collector.on_tool_use)
            self.hooks.register(HookEvent.POST_TOOL_FAILURE, self._interaction_collector.on_tool_use)
            logger.info("Knowledge synthesis system initialized")

    # =========================================================================
    # AGENT MANAGEMENT
    # =========================================================================

    async def _handle_agent_tunnel(self, ui_ws, msg):
        """058 (Mode 1 transport): unwrap a user agent's frame tunneled over its
        owner's authenticated UI socket and route it to the agent-message router
        via a stable TunnelSocket. The owner is the AUTHENTICATED session ``sub``
        — never anything the frame presents (FR-015). Flag-gated (byo_agents);
        inert when off (behavior byte-identical to today)."""
        if not flags.is_enabled("byo_agents"):
            return
        from shared.local_transport import TunnelSocket
        claims = self.ui_sessions.get(ui_ws) or {}
        owner_sub = claims.get("sub")
        payload = getattr(msg, "payload", None) or {}
        agent_id = payload.get("agent_id")
        inner = payload.get("frame")
        if not owner_sub or not agent_id or not inner:
            logger.debug("agent_tunnel: missing owner/agent/frame — ignoring")
            return
        if self._tunnel_ingress_over_cap(owner_sub):
            logger.warning("058: dropping tunnel frame from owner=%s agent=%s — over ingress cap",
                           owner_sub, agent_id)
            return
        # A socket that RELAYS an agent's stdio frames is a desktop host by
        # demonstration — it is supervising the child process right now. Mark it,
        # so a host that predates the explicit register_ui `agent_host` field is
        # still a valid delivery target (and a browser tab, which relays nothing,
        # still never is).
        _hosts = getattr(self, "_agent_host_sockets", None)
        if _hosts is not None:
            _hosts.setdefault(id(ui_ws), str(payload.get("host_session_id") or ""))
        key = (owner_sub, agent_id)
        sock = self._tunnel_sockets.get(key)
        if sock is None:
            sock = TunnelSocket(ui_ws, owner_sub, agent_id, self._safe_send)
            sock.host_session_id = payload.get("host_session_id")
            self._tunnel_sockets[key] = sock
        elif getattr(sock, "ui_websocket", None) is not ui_ws:
            # Reconnect on a different socket: supersede the stale one.
            sock.ui_websocket = ui_ws
            sock.host_session_id = payload.get("host_session_id")
        frame_text = inner if isinstance(inner, str) else json.dumps(inner)
        await self.handle_agent_message(sock, frame_text)

    async def _teardown_owner_tunnels(self, ui_ws):
        """058 (honest-offline, FR-010/FR-011): on UI disconnect, take every user
        agent tunneled over this socket OFFLINE — drop its TunnelSocket and live
        registration so a subsequent invocation returns a prompt honest-offline
        response. Notifies the owner's other sockets best-effort."""
        owner_sub = (self.ui_sessions.get(ui_ws) or {}).get("sub")
        gone = [(k, s) for k, s in list(self._tunnel_sockets.items())
                if getattr(s, "ui_websocket", None) is ui_ws]
        for key, sock in gone:
            agent_id = key[1]
            self._tunnel_sockets.pop(key, None)
            if self.agents.get(agent_id) is sock:
                self.agents.pop(agent_id, None)
            for other in list(self.ui_clients):
                if other is ui_ws or self._get_user_id(other) != owner_sub:
                    continue
                try:
                    await self._safe_send(other, json.dumps(
                        {"type": "agent_offline", "agent_id": agent_id}))
                except Exception:
                    logger.debug("agent_offline notify failed", exc_info=True)
        if gone:
            logger.info("058: %d user agent(s) offline on UI disconnect (owner=%s)",
                        len(gone), owner_sub)

    #: Max agent-tunnel frames per owner per window before dropping (058 FR-017).
    _TUNNEL_MAX_FRAMES_PER_WINDOW = int(os.getenv("BYO_TUNNEL_MAX_FRAMES_PER_S", "50"))
    _TUNNEL_WINDOW_S = 1.0

    def _tunnel_ingress_over_cap(self, owner_sub: str) -> bool:
        """Per-owner fixed-window frame-rate cap on the agent tunnel (058
        FR-017/SC-008). Returns True once this owner exceeds the window's frame
        budget — the caller drops the frame, so a flooding/runaway user agent
        degrades only its own owner, never the platform or other users. Each owner
        has an independent counter."""
        now = time.monotonic()
        st = self._tunnel_ingress.get(owner_sub)
        if st is None or (now - st[0]) >= self._TUNNEL_WINDOW_S:
            self._tunnel_ingress[owner_sub] = [now, 1]
            return False
        st[1] += 1
        return st[1] > self._TUNNEL_MAX_FRAMES_PER_WINDOW

    def is_agent_host_socket(self, websocket) -> bool:
        """058: did this UI socket declare itself a desktop AGENT HOST at
        register_ui? Only such a socket can write a bundle to disk and supervise
        it as a child process — and only such a socket is ever sent one."""
        return id(websocket) in (getattr(self, "_agent_host_sockets", None) or {})

    def owner_host_sockets(self, owner_sub) -> list:
        """This owner's live DESKTOP-HOST sockets (never a browser tab)."""
        return [ui for ui in list(self.ui_clients)
                if self.is_agent_host_socket(ui) and self._get_user_id(ui) == owner_sub]

    async def _refuse_personal_agent_host(
        self,
        websocket: Any,
        *,
        code: str,
        details: Dict[str, Any],
    ) -> None:
        """Send the exact non-disclosing v2 host-registration refusal."""

        await self._safe_send(
            websocket,
            json.dumps(
                {
                    "type": "agent_host_registration_refused",
                    "code": code,
                    "retryable": False,
                    "details": details,
                    "refused_at": self._rfc3339(),
                },
                separators=(",", ":"),
            ),
        )

    async def _register_personal_agent_host(
        self,
        websocket: Any,
        *,
        owner_user_id: str,
        registration: AgentHostRegistration,
    ) -> Any | None:
        """Durably validate and acknowledge one structured desktop host.

        Authentication and the finite connection scope are already established
        by the caller. No socket becomes delivery-eligible until the repository
        commits and this method emits the server-owned session acknowledgement.
        """

        from orchestrator.user_agents import HostRegistrationRefused

        context = (getattr(self, "_connection_contexts", None) or {}).get(
            id(websocket)
        )
        if context is None:
            # Direct unit seams predate the finite connection runtime. Product
            # WebSockets always have a context; a UUID4 test seam still exercises
            # the exact durable host contract without trusting a client value.
            connection_scope_id = str(_uuid.uuid4())
        else:
            connection_scope_id = str(context.connection_scope_id)
        try:
            record = await asyncio.to_thread(
                self.personal_agent_runtime.register_host_session,
                owner_user_id=owner_user_id,
                connection_scope_id=connection_scope_id,
                host_id=registration.host_id,
                platform=registration.platform,
                client_version=registration.client_version,
                supported_runtime_contract_versions=(
                    registration.supported_runtime_contract_versions
                ),
                runtime_lock_sha256=registration.runtime_lock_sha256,
            )
        except HostRegistrationRefused as exc:
            await self._refuse_personal_agent_host(
                websocket,
                code=exc.code,
                details=dict(exc.details),
            )
            return None

        # A reconnect of the same stable installation supersedes its prior
        # server session. Remove only the stale projection; the repository has
        # already fenced and settled the old session transactionally.
        sessions = getattr(self, "_personal_agent_host_sessions", None)
        session_sockets = getattr(self, "_personal_agent_session_sockets", None)
        if sessions is None:
            sessions = self._personal_agent_host_sessions = {}
        if session_sockets is None:
            session_sockets = self._personal_agent_session_sockets = {}
        for socket_id, prior in list(sessions.items()):
            if (
                prior.owner_user_id == owner_user_id
                and prior.host_id == record.host_id
                and prior.host_session_id != record.host_session_id
            ):
                sessions.pop(socket_id, None)
                session_sockets.pop(prior.host_session_id, None)
        sessions[id(websocket)] = record
        session_sockets[record.host_session_id] = websocket
        self._agent_host_sockets[id(websocket)] = record.host_session_id

        acknowledgement = AgentHostRegistered(
            host_id=record.host_id,
            host_session_id=record.host_session_id,
            inventory_required=True,
            accepted_at=self._rfc3339(record.accepted_at),
        )
        await self._safe_send(websocket, acknowledgement.to_json())
        return record

    @staticmethod
    def _strict_uuid4(value: Any, field_name: str) -> str:
        try:
            parsed = _uuid.UUID(str(value))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ProtocolValidationError(f"{field_name} must be UUID4") from exc
        if (
            not isinstance(value, str)
            or parsed.version != 4
            or parsed.variant != _uuid.RFC_4122
            or str(parsed) != value
        ):
            raise ProtocolValidationError(f"{field_name} must be canonical UUID4")
        return value

    @staticmethod
    def _validate_host_timestamp(value: Any, field_name: str) -> None:
        if not isinstance(value, str) or not value.endswith("Z"):
            raise ProtocolValidationError(f"{field_name} must be UTC")
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError as exc:
            raise ProtocolValidationError(f"{field_name} must be UTC") from exc
        if parsed.utcoffset() != timedelta(0):
            raise ProtocolValidationError(f"{field_name} must be UTC")

    def _bound_personal_agent_host(self, websocket: Any) -> Any:
        record = (getattr(self, "_personal_agent_host_sessions", None) or {}).get(
            id(websocket)
        )
        if record is None:
            raise ProtocolValidationError("socket has no acknowledged host session")
        bound_socket = (
            getattr(self, "_personal_agent_session_sockets", None) or {}
        ).get(record.host_session_id)
        if bound_socket is not websocket:
            raise ProtocolValidationError("host session belongs to another socket")
        return record

    @classmethod
    def _invalid_host_registration_field(cls, value: Any) -> str:
        expected = {
            "host_id",
            "supported_runtime_contract_versions",
            "runtime_lock_sha256",
            "platform",
            "client_version",
        }
        if not isinstance(value, dict) or set(value) != expected:
            return "agent_host"
        try:
            cls._strict_uuid4(value.get("host_id"), "host_id")
        except ProtocolValidationError:
            return "host_id"
        versions = value.get("supported_runtime_contract_versions")
        if (
            not isinstance(versions, list)
            or not versions
            or any(type(item) is not int or item <= 0 for item in versions)
            or versions != sorted(set(versions))
        ):
            return "supported_runtime_contract_versions"
        lock_digest = value.get("runtime_lock_sha256")
        if (
            not isinstance(lock_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", lock_digest) is None
        ):
            return "runtime_lock_sha256"
        if value.get("platform") not in {"windows", "macos"}:
            return "platform"
        client_version = value.get("client_version")
        if (
            not isinstance(client_version, str)
            or re.fullmatch(
                r"(0|[1-9][0-9]*)\."
                r"(0|[1-9][0-9]*)\."
                r"(0|[1-9][0-9]*)"
                r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
                r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?",
                client_version,
            )
            is None
        ):
            return "client_version"
        return "agent_host"

    @staticmethod
    def _fence_without_process(fence: RuntimeFence) -> RuntimeFence:
        return RuntimeFence(
            agent_id=fence.agent_id,
            host_id=fence.host_id,
            host_session_id=fence.host_session_id,
            delivery_id=fence.delivery_id,
            revision_id=fence.revision_id,
            runtime_instance_id=fence.runtime_instance_id,
            process_id=None,
            lifecycle_generation=fence.lifecycle_generation,
        )

    @staticmethod
    def _assert_host_fence(record: Any, fence: RuntimeFence) -> None:
        if not (
            fence.host_id == record.host_id
            and fence.host_session_id == record.host_session_id
            and record.state == "connected"
        ):
            raise ProtocolValidationError("host runtime fence is stale")

    async def _personal_agent_revision_metadata(
        self, revision_id: str
    ) -> Dict[str, Any]:
        row = await asyncio.to_thread(
            self.history.db.fetch_one,
            "SELECT artifact_digest, runtime_contract_version, "
            "release_lock_digest FROM user_agent_revision WHERE revision_id = ?",
            (revision_id,),
        )
        if row is None:
            raise ProtocolValidationError("runtime revision is unknown")
        return dict(row)

    async def _fail_personal_agent_waiters(
        self, request_ids: Any, *, code: str
    ) -> None:
        waiters = getattr(self, "_personal_agent_request_waiters", None) or {}
        for request_id in request_ids or ():
            waiter = waiters.get(str(request_id))
            if waiter is not None and not waiter.done():
                waiter.set_result(
                    MCPResponse(
                        request_id=str(request_id),
                        error={"message": code, "retryable": True, "code": code},
                    )
                )

    def _personal_agent_owner_for_fence(
        self, fence: RuntimeFence
    ) -> str | None:
        """Resolve an owner only from the acknowledged host-session binding."""

        for record in (
            getattr(self, "_personal_agent_host_sessions", None) or {}
        ).values():
            if record.host_session_id == fence.host_session_id:
                return str(record.owner_user_id)
        projected = (
            getattr(self, "_personal_agent_runtime_sockets", None) or {}
        ).get(fence.runtime_instance_id)
        owner = getattr(projected, "owner_sub", None)
        return owner if isinstance(owner, str) and owner else None

    @staticmethod
    def _canonical_personal_agent_failure_code(failure_code: str) -> str:
        """Map historical/internal diagnostics to the public stable contract."""

        aliases = {
            "child_failed": "child_exited",
            "child_exit": "child_exited",
            "child_offline": "agent_offline",
            "disconnected": "host_lost",
            "revision_activation_failed": "revision_promotion_failed",
            "standby_recovery_failed": "child_start_failed",
            "process_exit": "child_exited",
        }
        canonical = aliases.get(failure_code, failure_code)
        return (
            canonical
            if canonical in AGENT_LIFECYCLE_REASON_CODES
            else "agent_offline"
        )

    @classmethod
    def _personal_agent_terminal_lifecycle_state(cls, failure_code: str) -> str:
        canonical = cls._canonical_personal_agent_failure_code(failure_code)
        if canonical in {"host_lost", "agent_offline", "agent_deleted"}:
            return "offline"
        return "failed"

    async def _emit_personal_agent_lifecycle(
        self,
        owner_user_id: str | None,
        runtime: Any,
        *,
        state: str,
        reason_code: str | None = None,
    ) -> None:
        """Best-effort owner-scoped projection of one committed runtime row."""

        if not owner_user_id:
            return
        try:
            from orchestrator.agent_lifecycle import publish_agent_lifecycle

            await publish_agent_lifecycle(
                self,
                owner_user_id,
                agent_id=runtime.fence.agent_id,
                revision_id=runtime.fence.revision_id,
                runtime_instance_id=runtime.fence.runtime_instance_id,
                lifecycle_generation=runtime.fence.lifecycle_generation,
                state_revision=runtime.state_revision,
                state=state,
                reason_code=reason_code,
            )
        except Exception:
            logger.debug(
                "Personal-agent lifecycle projection failed agent=%s state=%s",
                runtime.fence.agent_id,
                state,
                exc_info=True,
            )

    @classmethod
    def _personal_agent_lifecycle_from_runtime(
        cls, runtime: Any
    ) -> tuple[str, str | None]:
        """Project one durable runtime row into the canonical public state."""

        state = str(getattr(runtime, "state", "offline"))
        if state in {"delivering", "starting"}:
            return "starting", None
        if state == "ready":
            # ``ready`` is asserted by the host after registration/liveness but
            # precedes the server-owned promotion.  A candidate replacing a
            # different durable active revision is visibly updating; a first
            # install or same-revision recovery is still starting.  Neither is
            # routable and neither may be projected as public online.
            active_revision_id = getattr(runtime, "active_revision_id", None)
            authoritative_instance_id = getattr(
                runtime, "authoritative_instance_id", None
            )
            if (
                (
                    isinstance(active_revision_id, str)
                    and active_revision_id
                    and active_revision_id != runtime.fence.revision_id
                )
                or (
                    isinstance(authoritative_instance_id, str)
                    and authoritative_instance_id
                    and authoritative_instance_id
                    != runtime.fence.runtime_instance_id
                )
            ):
                return "updating", None
            return "starting", None
        if state == "online":
            return "online", None
        if state == "updating":
            return "updating", None
        failure = getattr(runtime, "failure_code", None)
        reason = cls._canonical_personal_agent_failure_code(
            str(failure)
            if failure
            else ("child_exited" if state == "failed" else "agent_offline")
        )
        return cls._personal_agent_terminal_lifecycle_state(reason), reason

    async def _replay_personal_agent_lifecycles(
        self, websocket: Any, owner_user_id: str
    ) -> int:
        """Hydrate one reconnecting owner from durable lifecycle rows."""

        repository = getattr(self, "personal_agent_runtime", None)
        loader = getattr(repository, "list_latest_runtime_instances", None)
        if not callable(loader):
            return 0
        runtimes = await asyncio.to_thread(loader, owner_user_id=owner_user_id)
        from orchestrator.agent_lifecycle import canonical_agent_lifecycle

        delivered = 0
        for runtime in runtimes:
            state, reason_code = self._personal_agent_lifecycle_from_runtime(runtime)
            frame = canonical_agent_lifecycle(
                agent_id=runtime.fence.agent_id,
                revision_id=runtime.fence.revision_id,
                runtime_instance_id=runtime.fence.runtime_instance_id,
                lifecycle_generation=runtime.fence.lifecycle_generation,
                state_revision=runtime.state_revision,
                state=state,
                reason_code=reason_code,
                updated_at=getattr(runtime, "terminal_at", None)
                or getattr(runtime, "last_liveness_at", None)
                or getattr(runtime, "created_at", None),
            )
            if await self._safe_send(websocket, frame.to_json()):
                delivered += 1
        return delivered

    async def _terminalize_personal_agent_runtime(
        self,
        fence: RuntimeFence,
        *,
        failure_code: str,
    ) -> Any:
        failure_code = self._canonical_personal_agent_failure_code(failure_code)
        owner_user_id = self._personal_agent_owner_for_fence(fence)
        result = await asyncio.to_thread(
            self.personal_agent_runtime.terminalize_runtime,
            fence,
            failure_code=failure_code,
        )
        ready = (getattr(self, "_personal_agent_ready_waiters", None) or {}).get(
            fence.runtime_instance_id
        )
        if ready is not None and not ready.done():
            ready.set_exception(RuntimeError(failure_code))
        await self._fail_personal_agent_waiters(
            result.settled_request_ids, code=failure_code
        )
        socket = (getattr(self, "_personal_agent_runtime_sockets", None) or {}).pop(
            fence.runtime_instance_id, None
        )
        if socket is not None and self.agents.get(fence.agent_id) is socket:
            self.agents.pop(fence.agent_id, None)
        await self._emit_personal_agent_lifecycle(
            owner_user_id,
            result.instance,
            state=self._personal_agent_terminal_lifecycle_state(failure_code),
            reason_code=failure_code,
        )
        return result

    async def _disconnect_personal_agent_host(self, websocket: Any) -> Any | None:
        sessions = getattr(self, "_personal_agent_host_sessions", None) or {}
        record = sessions.get(id(websocket))
        if record is None:
            return None
        try:
            result = await asyncio.to_thread(
                self.personal_agent_runtime.disconnect_host_session,
                record.fence,
                failure_code="host_lost",
            )
        except Exception:
            logger.warning("Personal-agent host disconnect failed closed", exc_info=True)
            return None

        # PostgreSQL loss/settlement is the authority boundary.  Only after it
        # commits may the process-local socket projections disappear; otherwise
        # a transient database failure could strand a still-selected durable
        # session while making it look disconnected in this process.
        if sessions.get(id(websocket)) == record:
            sessions.pop(id(websocket), None)
        session_sockets = (
            getattr(self, "_personal_agent_session_sockets", None) or {}
        )
        if session_sockets.get(record.host_session_id) is websocket:
            session_sockets.pop(record.host_session_id, None)
        (getattr(self, "_agent_host_sockets", None) or {}).pop(
            id(websocket), None
        )
        await self._fail_personal_agent_waiters(
            result.settled_request_ids, code="host_lost"
        )
        for settlement in getattr(result, "settlements", ()):
            await self._emit_personal_agent_lifecycle(
                record.owner_user_id,
                settlement.instance,
                state="offline",
                reason_code="host_lost",
            )
        for runtime_id, socket in list(
            (getattr(self, "_personal_agent_runtime_sockets", None) or {}).items()
        ):
            if getattr(socket, "ui_websocket", None) is websocket:
                self._personal_agent_runtime_sockets.pop(runtime_id, None)
                if self.agents.get(getattr(socket, "agent_id", None)) is socket:
                    self.agents.pop(socket.agent_id, None)

        # The disconnect transaction has already selected every replacement
        # standby and fenced the lost authority. Re-open the exact immutable
        # artifact before asking only that selected session to start a fresh
        # delivery/runtime generation. Recovery failures are isolated per
        # agent and durably terminalize their allocated delivery operation.
        selected_recoveries = [
            (agent_id, selected_session_id)
            for agent_id, selected_session_id in result.selected_sessions.items()
            if selected_session_id is not None
        ]
        if selected_recoveries:
            recovery_limit = asyncio.Semaphore(8)

            async def recover(agent_id: str, selected_session_id: str) -> None:
                async with recovery_limit:
                    await self._recover_personal_agent_on_selected_standby(
                        owner_user_id=record.owner_user_id,
                        agent_id=agent_id,
                        lost_host_session_id=record.host_session_id,
                        selected_host_session_id=selected_session_id,
                    )

            await asyncio.gather(
                *(recover(agent_id, selected_session_id)
                  for agent_id, selected_session_id in selected_recoveries)
            )
        return result

    async def _recover_personal_agent_on_selected_standby(
        self,
        *,
        owner_user_id: str,
        agent_id: str,
        lost_host_session_id: str,
        selected_host_session_id: str,
    ) -> bool:
        """Deliver one active immutable revision to its selected standby.

        Durable selection/allocation remains in PostgreSQL. The filesystem is
        consulted only after that transaction returns the exact revision, and
        the bytes are re-hashed before they cross the selected host socket.
        """

        recovery_identity = {
            "agent_id": agent_id,
            "lost_host_session_id": lost_host_session_id,
            "selected_host_session_id": selected_host_session_id,
        }
        idempotency_key = hashlib.sha256(
            json.dumps(
                recovery_identity,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        try:
            claimed = await self._claim_personal_agent_operation(
                owner_user_id=owner_user_id,
                operation_kind="agent_runtime_delivery",
                idempotency_namespace="personal_agent_standby_recovery",
                idempotency_key=idempotency_key,
                normalized_identity=recovery_identity,
                wait_seconds=5.0,
            )
        except Exception:
            logger.warning(
                "Personal-agent standby recovery admission failed agent=%s",
                agent_id,
                exc_info=True,
            )
            return False
        if claimed is None:
            logger.warning(
                "Personal-agent standby recovery was not admitted agent=%s",
                agent_id,
            )
            return False

        _operation_owner, operation_claim = claimed
        recovery = None
        try:
            recovery = await asyncio.to_thread(
                self.personal_agent_runtime.create_selected_recovery_instance,
                owner_user_id=owner_user_id,
                agent_id=agent_id,
                operation_fence=operation_claim.fence,
            )
            if recovery.host.host_session_id != selected_host_session_id:
                raise RuntimeError("selected standby changed before recovery")

            artifact_store = getattr(self, "personal_agent_artifacts", None)
            if artifact_store is None:
                from orchestrator.artifact_publication import (
                    ImmutableAgentArtifactStore,
                )

                artifact_store = self.personal_agent_artifacts = (
                    ImmutableAgentArtifactStore()
                )
            artifact = await asyncio.to_thread(
                artifact_store.load,
                recovery.revision.artifact_relative_path,
                expected_digest=recovery.revision.artifact_digest,
            )
            if not (
                artifact.manifest.get("runtime_contract_version")
                == recovery.revision.runtime_contract_version
                and artifact.manifest.get("required_runtime_lock_sha256")
                == recovery.revision.release_lock_digest
            ):
                raise RuntimeError("standby artifact compatibility metadata is stale")
            selected_socket = (
                getattr(self, "_personal_agent_session_sockets", None) or {}
            ).get(selected_host_session_id)
            if selected_socket is None:
                raise RuntimeError("selected standby socket is unavailable")
            if not await self._safe_send(
                selected_socket,
                json.dumps(
                    {
                        "type": "agent_bundle_deliver",
                        "fence": recovery.instance.fence.to_dict(),
                        "runtime_contract_version": (
                            recovery.revision.runtime_contract_version
                        ),
                        "required_runtime_lock_sha256": (
                            recovery.revision.release_lock_digest
                        ),
                        "bundle_sha256": artifact.bundle_sha256,
                        "files": dict(artifact.files),
                    },
                    separators=(",", ":"),
                ),
            ):
                raise RuntimeError("selected standby send failed")
            await self._emit_personal_agent_lifecycle(
                owner_user_id,
                recovery.instance,
                state="starting",
            )
            return True
        except Exception:
            if recovery is not None:
                try:
                    await self._terminalize_personal_agent_runtime(
                        recovery.instance.fence,
                        failure_code="standby_recovery_failed",
                    )
                except Exception:
                    logger.debug(
                        "Standby recovery runtime was already terminal",
                        exc_info=True,
                    )
            else:
                try:
                    await self._call_work_admission(
                        self.work_admission.terminalize,
                        operation_claim.fence,
                        state=OperationState.RETRYABLE,
                        terminal_code="standby_recovery_failed",
                        safe_summary="Personal-agent standby recovery failed",
                        retry_after_ms=0,
                    )
                except Exception:
                    logger.debug(
                        "Standby recovery operation was already terminal",
                        exc_info=True,
                    )
            logger.warning(
                "Personal-agent standby recovery failed agent=%s selected=%s",
                agent_id,
                selected_host_session_id,
                exc_info=True,
            )
            return False

    async def _claim_personal_agent_operation(
        self,
        *,
        owner_user_id: str,
        operation_kind: str,
        idempotency_namespace: str,
        idempotency_key: str,
        normalized_identity: Dict[str, Any],
        admission_class: AdmissionClass = AdmissionClass.BACKGROUND,
        parent_operation_id: _uuid.UUID | None = None,
        request_generation: _uuid.UUID | None = None,
        wait_seconds: float = 2.0,
    ) -> tuple[OperationOwner, Any] | None:
        owner = OperationOwner(
            owner_scope=OwnerScope.USER,
            owner_user_id=owner_user_id,
            connection_scope_id=None,
        )
        digest = hashlib.sha256(
            json.dumps(
                normalized_identity,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        request = OperationRequest(
            operation_kind=operation_kind,
            admission_class=admission_class,
            owner=owner,
            submission_id=_uuid.uuid4(),
            idempotency_namespace=idempotency_namespace,
            idempotency_key=idempotency_key,
            normalized_input_digest=digest,
            chat_id=None,
            parent_operation_id=parent_operation_id,
            connection_generation=None,
            request_generation=request_generation,
        )
        admitted = await self._call_work_admission(
            self.work_admission.submit, request
        )
        if not admitted.accepted:
            return None
        deadline = time.monotonic() + wait_seconds
        while True:
            claim = await self._call_work_admission(
                self.work_admission.claim_operation,
                admission_class,
                admitted.operation_id,
            )
            if claim is not None:
                return owner, claim
            projection = await self._call_work_admission(
                self.work_admission.query_operation,
                owner=owner,
                operation_id=admitted.operation_id,
            )
            if projection.state in {
                OperationState.COMPLETED,
                OperationState.FAILED,
                OperationState.CANCELLED,
                OperationState.RETRYABLE,
            }:
                return None
            if time.monotonic() >= deadline:
                await self._call_work_admission(
                    self.work_admission.terminalize_unselected,
                    admitted.operation_id,
                    terminal_code="capacity_exceeded",
                    safe_summary="Personal-agent operation was not selected",
                    retry_after_ms=1000,
                )
                return None
            await asyncio.sleep(0.05)

    async def _renew_personal_agent_operation_lease(
        self,
        fence: ExecutionFence,
        stop: asyncio.Event,
    ) -> None:
        """Keep a selected personal-agent operation current until settlement.

        Tool calls may legitimately outlive the admission slot's default lease.
        Renewal therefore begins immediately after assignment and remains active
        through the durable request-settlement transaction. A stale lease is
        already a fail-closed authority loss; the eventual result/timeout path
        cannot publish through that obsolete execution fence.
        """

        interval = max(0.001, CONNECTION_LEASE_RENEW_SECONDS)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                await self._call_work_admission(
                    self.work_admission.renew_execution_lease,
                    fence,
                )
            except StaleExecutionFenceError:
                logger.warning(
                    "Personal-agent operation lease was lost operation_id=%s",
                    fence.operation_id,
                )
                return
            except Exception:
                # A transient database failure is retried on the next bounded
                # interval. PostgreSQL remains authoritative; this task never
                # invents or reselects an execution generation in memory.
                logger.warning(
                    "Personal-agent operation lease renewal failed",
                    exc_info=True,
                )

    async def _reconcile_personal_agent_inventory(
        self,
        websocket: Any,
        frame: Dict[str, Any],
    ) -> None:
        """Validate and atomically reconcile one complete retained inventory."""

        if set(frame) != {
            "type",
            "host_id",
            "host_session_id",
            "inventory_id",
            "entries",
        }:
            raise ProtocolValidationError("host inventory fields are invalid")
        record = self._bound_personal_agent_host(websocket)
        if not (
            frame["host_id"] == record.host_id
            and frame["host_session_id"] == record.host_session_id
        ):
            raise ProtocolValidationError("host inventory session is stale")
        inventory_id = self._strict_uuid4(frame["inventory_id"], "inventory_id")
        entries = frame["entries"]
        if not isinstance(entries, list):
            raise ProtocolValidationError("host inventory entries must be an array")

        expected_entry_fields = {
            "agent_id",
            "revision_id",
            "bundle_sha256",
            "runtime_contract_version",
            "required_runtime_lock_sha256",
        }
        seen: set[tuple[str, str]] = set()
        delivery_fences: Dict[tuple[str, str], ExecutionFence] = {}
        claimed_operations: list[tuple[OperationOwner, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != expected_entry_fields:
                raise ProtocolValidationError("host inventory entry is invalid")
            revision_id = self._strict_uuid4(entry["revision_id"], "revision_id")
            agent_id = entry["agent_id"]
            if not isinstance(agent_id, str) or not agent_id or len(agent_id) > 255:
                raise ProtocolValidationError("inventory agent_id is invalid")
            key = (agent_id, revision_id)
            if key in seen:
                raise ProtocolValidationError("host inventory entry is duplicated")
            seen.add(key)
            if (
                not isinstance(entry["bundle_sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", entry["bundle_sha256"]) is None
                or not isinstance(entry["required_runtime_lock_sha256"], str)
                or re.fullmatch(
                    r"[0-9a-f]{64}", entry["required_runtime_lock_sha256"]
                )
                is None
                or type(entry["runtime_contract_version"]) is not int
            ):
                raise ProtocolValidationError("inventory compatibility data is invalid")
            try:
                selected = await asyncio.to_thread(
                    self.personal_agent_runtime.get_selected_session_revision,
                    record.fence,
                    agent_id=agent_id,
                )
            except Exception:
                continue
            revision = selected.revision
            if not (
                revision.revision_id == revision_id
                and revision.artifact_digest == entry["bundle_sha256"]
                and revision.runtime_contract_version
                == entry["runtime_contract_version"]
                and revision.release_lock_digest
                == entry["required_runtime_lock_sha256"]
                and revision.state == "active"
            ):
                continue
            operation = await self._claim_personal_agent_operation(
                owner_user_id=record.owner_user_id,
                operation_kind="agent_runtime_delivery",
                idempotency_namespace="personal_agent_inventory_delivery",
                idempotency_key=f"{inventory_id}:{agent_id}:{revision_id}",
                normalized_identity={
                    "inventory_id": inventory_id,
                    "agent_id": agent_id,
                    "revision_id": revision_id,
                    "host_session_id": record.host_session_id,
                },
            )
            if operation is None:
                raise RuntimeError("personal-agent inventory delivery was not admitted")
            claimed_operations.append(operation)
            delivery_fences[key] = operation[1].fence

        try:
            reconciliation = await asyncio.to_thread(
                self.personal_agent_runtime.reconcile_host_inventory,
                record.fence,
                inventory_id=inventory_id,
                entries=entries,
                delivery_operation_fences=delivery_fences,
            )
        except Exception:
            for _owner, claim in claimed_operations:
                try:
                    await self._call_work_admission(
                        self.work_admission.terminalize,
                        claim.fence,
                        state=OperationState.RETRYABLE,
                        terminal_code="inventory_reconciliation_failed",
                        safe_summary="Host inventory reconciliation failed",
                        retry_after_ms=0,
                    )
                except Exception:
                    logger.debug("inventory operation cleanup failed", exc_info=True)
            raise

        actions = []
        for action in reconciliation.actions:
            selected = action.selected_delivery
            actions.append(
                {
                    "agent_id": action.agent_id,
                    "revision_id": action.revision_id,
                    "action": action.action,
                    "reason_code": action.reason_code,
                    "selected_delivery": (
                        None
                        if selected is None
                        else {
                            "delivery_id": selected.delivery_id,
                            "runtime_instance_id": selected.runtime_instance_id,
                            "lifecycle_generation": selected.lifecycle_generation,
                            "runtime_contract_version": (
                                selected.runtime_contract_version
                            ),
                            "required_runtime_lock_sha256": (
                                selected.required_runtime_lock_sha256
                            ),
                            "bundle_sha256": selected.bundle_sha256,
                        }
                    ),
                }
            )
        inventory_sent = await self._safe_send(
            websocket,
            json.dumps(
                {
                    "type": "agent_host_inventory_reconciled",
                    "host_id": reconciliation.host.host_id,
                    "host_session_id": reconciliation.host.host_session_id,
                    "inventory_id": reconciliation.inventory_id,
                    "actions": actions,
                    "reconciled_at": self._rfc3339(reconciliation.reconciled_at),
                },
                separators=(",", ":"),
            ),
        )
        if inventory_sent:
            for action in reconciliation.actions:
                selected = action.selected_delivery
                if selected is None or action.action != "start":
                    continue
                try:
                    starting_runtime = await asyncio.to_thread(
                        self.personal_agent_runtime.get_runtime_instance,
                        selected.runtime_instance_id,
                    )
                except Exception:
                    logger.debug(
                        "Inventory lifecycle runtime lookup failed",
                        exc_info=True,
                    )
                    continue
                await self._emit_personal_agent_lifecycle(
                    record.owner_user_id,
                    starting_runtime,
                    state="starting",
                )

    async def _personal_agent_watchdog_once(self) -> int:
        """Fence runtimes whose PostgreSQL receipt-time liveness expired."""

        rows = await asyncio.to_thread(
            self.history.db.fetch_all,
            """
            SELECT runtime_instance_id,
                   state
            FROM agent_runtime_instance
            WHERE (
                state IN ('delivering', 'starting')
                AND COALESCE(started_at, created_at) <
                    clock_timestamp() - (? * interval '1 second')
            ) OR (
                state IN ('ready', 'online', 'updating')
                AND last_liveness_at IS NOT NULL
                AND last_liveness_at <
                    clock_timestamp() - (? * interval '1 second')
            )
            ORDER BY runtime_instance_id
            """,
            (
                PERSONAL_AGENT_STARTUP_TIMEOUT_SECONDS,
                PERSONAL_AGENT_HEARTBEAT_TIMEOUT_SECONDS,
            ),
        )
        fenced = 0
        for row in rows:
            try:
                runtime = await asyncio.to_thread(
                    self.personal_agent_runtime.get_runtime_instance,
                    str(row["runtime_instance_id"]),
                )
                if str(row["state"]) in {"delivering", "starting"}:
                    settlement = await asyncio.to_thread(
                        self.personal_agent_runtime.terminalize_expired_startup,
                        runtime.fence,
                        timeout_seconds=PERSONAL_AGENT_STARTUP_TIMEOUT_SECONDS,
                    )
                else:
                    settlement = await asyncio.to_thread(
                        self.personal_agent_runtime.terminalize_expired_liveness,
                        runtime.fence,
                        timeout_seconds=PERSONAL_AGENT_HEARTBEAT_TIMEOUT_SECONDS,
                    )
                owner_user_id = self._personal_agent_owner_for_fence(
                    runtime.fence
                )
                await self._fail_personal_agent_waiters(
                    settlement.settled_request_ids,
                    code=(
                        "child_registration_timeout"
                        if str(row["state"]) in {"delivering", "starting"}
                        else "child_hung"
                    ),
                )
                ready = (
                    getattr(self, "_personal_agent_ready_waiters", None) or {}
                ).get(runtime.fence.runtime_instance_id)
                if ready is not None and not ready.done():
                    ready.set_exception(
                        RuntimeError(settlement.instance.failure_code)
                    )
                projected = (
                    getattr(self, "_personal_agent_runtime_sockets", None) or {}
                ).pop(runtime.fence.runtime_instance_id, None)
                if (
                    projected is not None
                    and self.agents.get(runtime.fence.agent_id) is projected
                ):
                    self.agents.pop(runtime.fence.agent_id, None)
                host_socket = (
                    getattr(self, "_personal_agent_session_sockets", None) or {}
                ).get(runtime.fence.host_session_id)
                if host_socket is not None and runtime.fence.process_id is not None:
                    await self._safe_send(
                        host_socket,
                        json.dumps(
                            {
                                "type": "agent_stop",
                                "fence": runtime.fence.to_dict(),
                            },
                            separators=(",", ":"),
                        ),
                    )
                await self._emit_personal_agent_lifecycle(
                    owner_user_id,
                    settlement.instance,
                    state=self._personal_agent_terminal_lifecycle_state(
                        settlement.instance.failure_code
                    ),
                    reason_code=settlement.instance.failure_code,
                )
                fenced += 1
            except Exception:
                # Another frame/watchdog may have won the same terminal CAS.
                logger.debug("personal-agent watchdog race lost", exc_info=True)
        return fenced

    async def _personal_agent_watchdog_loop(self) -> None:
        while True:
            try:
                await self._personal_agent_watchdog_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("personal-agent watchdog pass failed", exc_info=True)
            await asyncio.sleep(PERSONAL_AGENT_WATCHDOG_INTERVAL_SECONDS)

    async def _remote_job_poll_loop(self) -> None:
        """Feature 063 US4: always-on background poller for open remote Slurm jobs.
        Read-only by construction (only squeue/sacct/tail run); the loop must never
        die on a single bad pass."""
        while True:
            try:
                from orchestrator import remote_jobs
                await remote_jobs.poll_once(self)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("remote-job poll pass failed", exc_info=True)
            await asyncio.sleep(REMOTE_CLUSTER_POLL_INTERVAL_SECONDS)

    async def _handle_personal_agent_result(
        self,
        websocket: Any,
        frame: Dict[str, Any],
    ) -> None:
        allowed = {
            "type",
            "request_id",
            "request_generation",
            "fence",
            "result",
            "error",
            "ui_components",
            "correlation_id",
            "result_type",
            "responder_info",
        }
        required = {"type", "request_id", "request_generation", "fence"}
        if not required.issubset(frame) or not set(frame).issubset(allowed):
            raise ProtocolValidationError("personal-agent response fields are invalid")
        record = self._bound_personal_agent_host(websocket)
        fence = RuntimeFence.from_dict(frame["fence"])
        self._assert_host_fence(record, fence)
        request_id = self._strict_uuid4(frame["request_id"], "request_id")
        request_generation = self._strict_uuid4(
            frame["request_generation"], "request_generation"
        )
        request = await asyncio.to_thread(
            self.personal_agent_runtime.get_runtime_request, request_id
        )
        if not (
            request.fence.runtime == fence
            and request.fence.request_generation == request_generation
        ):
            raise ProtocolValidationError("personal-agent response fence is stale")
        error = frame.get("error")
        if error is None:
            terminal_state = "completed"
            terminal_code = None
            canonical = json.dumps(
                {
                    "result": frame.get("result"),
                    "ui_components": frame.get("ui_components"),
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            result_digest = hashlib.sha256(canonical).hexdigest()
        else:
            if not isinstance(error, dict):
                raise ProtocolValidationError("personal-agent error must be an object")
            terminal_state = "retryable" if error.get("retryable") is True else "failed"
            terminal_code = (
                "agent_retryable" if terminal_state == "retryable" else "agent_error"
            )
            result_digest = None
        await asyncio.to_thread(
            self.personal_agent_runtime.settle_request,
            request.fence,
            state=terminal_state,
            terminal_code=terminal_code,
            result_digest=result_digest,
        )
        waiter = (
            getattr(self, "_personal_agent_request_waiters", None) or {}
        ).get(request_id)
        if waiter is not None and not waiter.done():
            waiter.set_result(
                MCPResponse(
                    request_id=request_id,
                    result=None if error is not None else frame.get("result"),
                    error=error,
                    ui_components=None if error is not None else frame.get("ui_components"),
                    correlation_id=frame.get("correlation_id"),
                    result_type=frame.get("result_type", "complete"),
                    responder_info=frame.get("responder_info"),
                )
            )

    async def _publish_personal_agent_runtime(self, runtime: Any) -> Any:
        """Expose one already-online durable authority to the tool router."""

        from shared.local_transport import FencedTunnelSocket

        fence = runtime.fence
        websocket = (
            getattr(self, "_personal_agent_session_sockets", None) or {}
        ).get(fence.host_session_id)
        if websocket is None:
            raise RuntimeError("selected personal-agent host is offline")
        host = self._bound_personal_agent_host(websocket)
        self._assert_host_fence(host, fence)
        card = (
            getattr(self, "_personal_agent_candidate_cards", None) or {}
        ).get(fence.runtime_instance_id)
        if card is None:
            raise RuntimeError("personal-agent runtime has no accepted card")
        socket = FencedTunnelSocket(
            websocket,
            host.owner_user_id,
            fence,
            self._safe_send,
        )
        self._personal_agent_runtime_sockets[fence.runtime_instance_id] = socket
        await self.register_agent(socket, RegisterAgent(agent_card=card))
        await self._emit_personal_agent_lifecycle(
            host.owner_user_id,
            runtime,
            state="online",
        )
        return socket

    async def _handle_personal_agent_host_frame(
        self,
        websocket: Any,
        frame: Dict[str, Any],
    ) -> None:
        """Reduce one exact v2 host frame through the durable repository."""

        try:
            frame_type = frame.get("type")
            if frame_type == "agent_host_inventory":
                await self._reconcile_personal_agent_inventory(websocket, frame)
                return
            if frame_type == "mcp_response":
                await self._handle_personal_agent_result(websocket, frame)
                return

            record = self._bound_personal_agent_host(websocket)
            if not isinstance(frame.get("fence"), dict):
                raise ProtocolValidationError("runtime frame fence is required")
            fence = RuntimeFence.from_dict(frame["fence"])
            self._assert_host_fence(record, fence)

            if frame_type == "agent_runtime_state":
                if set(frame) != {
                    "type",
                    "fence",
                    "state",
                    "runtime_contract_version",
                    "bundle_sha256",
                    "observed_at",
                    "reason_code",
                }:
                    raise ProtocolValidationError("runtime-state fields are invalid")
                state = frame["state"]
                if state not in {"starting", "ready", "failed", "offline"}:
                    raise ProtocolValidationError("runtime state is invalid")
                self._validate_host_timestamp(frame["observed_at"], "observed_at")
                reason = frame["reason_code"]
                if reason is not None and (
                    not isinstance(reason, str)
                    or re.fullmatch(r"[a-z][a-z0-9_]{0,127}", reason) is None
                ):
                    raise ProtocolValidationError("runtime reason is invalid")
                revision = await self._personal_agent_revision_metadata(
                    fence.revision_id
                )
                if not (
                    frame["runtime_contract_version"]
                    == int(revision["runtime_contract_version"])
                    and frame["bundle_sha256"] == revision["artifact_digest"]
                ):
                    raise ProtocolValidationError("runtime compatibility fence is stale")
                if state == "starting":
                    current = await asyncio.to_thread(
                        self.personal_agent_runtime.get_runtime_instance,
                        fence.runtime_instance_id,
                    )
                    if self._fence_without_process(fence) != current.fence:
                        raise ProtocolValidationError("prelaunch runtime fence is stale")
                    await asyncio.to_thread(
                        self.personal_agent_runtime.bind_runtime_process,
                        current.fence,
                        process_id=fence.process_id,
                        expected_state_revision=current.state_revision,
                    )
                    return
                if state == "ready":
                    ready_runtime = await asyncio.to_thread(
                        self.personal_agent_runtime.mark_runtime_ready, fence
                    )
                    waiter = (
                        getattr(self, "_personal_agent_ready_waiters", None) or {}
                    ).get(fence.runtime_instance_id)
                    if waiter is not None and not waiter.done():
                        waiter.set_result(ready_runtime.fence.runtime_instance_id)
                    elif hasattr(
                        self.personal_agent_runtime, "promote_recovered_runtime"
                    ):
                        # Inventory recovery uses an already-active immutable
                        # revision, so there is no candidate-revision promotion
                        # waiter. Restore only its runtime authority pointer.
                        online = await asyncio.to_thread(
                            self.personal_agent_runtime.promote_recovered_runtime,
                            ready_runtime.fence,
                        )
                        await self._publish_personal_agent_runtime(online)
                    return
                await self._terminalize_personal_agent_runtime(
                    fence,
                    failure_code=reason or (
                        "child_exited" if state == "failed" else "agent_offline"
                    ),
                )
                return

            if frame_type == "agent_runtime_register":
                if set(frame) != {
                    "type",
                    "fence",
                    "runtime_contract_version",
                    "bundle_sha256",
                    "agent_card",
                }:
                    raise ProtocolValidationError("runtime-register fields are invalid")
                card_value = frame["agent_card"]
                if not isinstance(card_value, dict) or set(card_value) != {
                    "name",
                    "description",
                    "agent_id",
                    "version",
                    "skills",
                    "metadata",
                }:
                    raise ProtocolValidationError("runtime agent card is invalid")
                card = AgentCard.from_dict(card_value)
                if card.agent_id != fence.agent_id:
                    raise ProtocolValidationError("runtime agent identity is stale")
                await asyncio.to_thread(
                    self.personal_agent_runtime.accept_runtime_registration,
                    fence,
                    runtime_contract_version=frame["runtime_contract_version"],
                    bundle_sha256=frame["bundle_sha256"],
                )
                self._personal_agent_candidate_cards[
                    fence.runtime_instance_id
                ] = card
                return

            if frame_type == "agent_runtime_heartbeat":
                if set(frame) != {"type", "fence", "heartbeat_sequence"}:
                    raise ProtocolValidationError("runtime-heartbeat fields are invalid")
                await asyncio.to_thread(
                    self.personal_agent_runtime.record_runtime_heartbeat,
                    fence,
                    heartbeat_sequence=frame["heartbeat_sequence"],
                )
                return

            if frame_type == "agent_runtime_exit":
                if set(frame) != {"type", "fence", "exit_kind", "exit_code"}:
                    raise ProtocolValidationError("runtime-exit fields are invalid")
                exit_kind = frame["exit_kind"]
                exit_code = frame["exit_code"]
                if exit_kind not in {"process_exit", "protocol_eof", "explicit_stop"}:
                    raise ProtocolValidationError("runtime exit kind is invalid")
                if (exit_kind == "process_exit") != (type(exit_code) is int):
                    raise ProtocolValidationError("runtime exit code is invalid")
                await self._terminalize_personal_agent_runtime(
                    fence,
                    failure_code={
                        "process_exit": "child_exited",
                        "protocol_eof": "child_exited",
                        "explicit_stop": "agent_offline",
                    }[exit_kind],
                )
                return
            raise ProtocolValidationError("unknown personal-agent host frame")
        except (
            ProtocolValidationError,
            ValueError,
        ) as exc:
            # Stale/malformed host frames are no-ops by contract. Do not echo
            # attacker-controlled values or allow arrival order to promote them.
            logger.warning(
                "Dropping personal-agent host frame type=%s: %s",
                frame.get("type"),
                type(exc).__name__,
            )
        except Exception:
            logger.warning(
                "Personal-agent host frame failed closed type=%s",
                frame.get("type"),
                exc_info=True,
            )

    def _is_user_agent(self, agent_id) -> bool:
        """True iff ``agent_id`` is a feature-057/058 user-created agent (has a
        ``user_agent`` registry row). Sync DB read — call via ``asyncio.to_thread``
        off the loop. Fails closed (False) so a lookup error never mislabels a
        built-in as a user agent."""
        if not agent_id:
            return False
        try:
            from orchestrator.user_agents import get_user_agent
            return get_user_agent(self.history.db, agent_id) is not None
        except Exception:
            return False

    async def _audit_user_agent(self, actor_sub, action_type, description,
                                agent_id, outcome="success"):
        """058 (T035/FR-012): record a user-agent lifecycle/denial audit row,
        attributed to the OWNING HUMAN. Best-effort, never raises — auditability
        must not break a delivery/registration/dispatch. Mirrors the 027
        ``agentic_creation._audit`` shape under the shared ``agent_lifecycle``
        class so the two creation lifecycles reconstruct together."""
        try:
            from datetime import datetime, timezone

            from audit.recorder import get_recorder
            from audit.schemas import AuditEventCreate
            rec = get_recorder()
            if rec is None:
                return
            who = actor_sub or "unknown"
            await rec.record(AuditEventCreate(
                actor_user_id=who,
                auth_principal=who,
                agent_id=agent_id,
                event_class="agent_lifecycle",
                action_type=action_type,
                description=(description or "user-agent event")[:1024] or "user-agent event",
                correlation_id=str(_uuid.uuid4()),
                outcome=outcome,
                inputs_meta={},
                started_at=datetime.now(timezone.utc),
            ))
        except Exception:
            logger.debug("058: user-agent audit record failed (%s)", action_type,
                         exc_info=True)

    async def deliver_agent_bundle(
        self,
        owner_sub,
        agent_id,
        files,
        constitution_version=None,
        *,
        runtime_manifest=None,
        bundle_sha256=None,
        revision_id=None,
        artifact_relative_path=None,
        runtime_contract_version=None,
        required_runtime_lock_sha256=None,
    ):
        """Prepare, deliver, and promote one immutable personal-agent revision.

        A legacy feature-058 bundle without v2 metadata keeps its compatibility
        behavior. Production generation always supplies the finalized manifest
        and follows the durable prepare/start/ready/promote boundary below.
        """
        from orchestrator.agent_generator import BYO_BUNDLE_FILENAMES

        if runtime_manifest is None and isinstance(files, dict):
            manifest_text = files.get("manifest.json")
            if isinstance(manifest_text, str):
                try:
                    parsed_manifest = json.loads(manifest_text)
                except (TypeError, ValueError):
                    parsed_manifest = None
                # Feature-058 compatibility bundles also contain a
                # ``manifest.json`` file, and some fixtures intentionally use
                # an empty object.  Only infer the durable v2 path when the
                # embedded manifest actually carries the complete v2 runtime
                # identity.  An explicitly supplied ``runtime_manifest`` still
                # enters the v2 validator and fails closed if malformed.
                v2_identity = {
                    "runtime_contract_version",
                    "revision_id",
                    "bundle_sha256",
                    "required_runtime_lock_sha256",
                }
                if (
                    isinstance(parsed_manifest, dict)
                    and parsed_manifest.get("runtime_contract_version") == 2
                    and v2_identity.issubset(parsed_manifest)
                ):
                    runtime_manifest = parsed_manifest
        if runtime_manifest is not None:
            manifest = dict(runtime_manifest)
            bundle_sha256 = bundle_sha256 or manifest.get("bundle_sha256")
            revision_id = revision_id or manifest.get("revision_id")
            runtime_contract_version = (
                runtime_contract_version
                if runtime_contract_version is not None
                else manifest.get("runtime_contract_version")
            )
            required_runtime_lock_sha256 = (
                required_runtime_lock_sha256
                or manifest.get("required_runtime_lock_sha256")
            )
            executable_files = {
                name: files[name]
                for name in BYO_BUNDLE_FILENAMES
                if isinstance(files, dict) and name in files
            }
            if set(executable_files) != set(BYO_BUNDLE_FILENAMES):
                raise ValueError("v2 personal-agent delivery requires exactly three files")
            return await self._deliver_personal_agent_revision(
                owner_sub=owner_sub,
                agent_id=agent_id,
                files=executable_files,
                runtime_manifest=manifest,
                bundle_sha256=bundle_sha256,
                revision_id=revision_id,
                artifact_relative_path=artifact_relative_path,
                runtime_contract_version=runtime_contract_version,
                required_runtime_lock_sha256=required_runtime_lock_sha256,
            )

        # Explicit compatibility path for old clients/tests. It never treats an
        # implicit v1 bundle as v2 and never enters the durable v2 host maps.
        frame = json.dumps({
            "type": "agent_bundle_deliver",
            "agent_id": agent_id,
            "files": files,
            "constitution_version": constitution_version,
        })
        delivered = 0
        for ui in self.owner_host_sockets(owner_sub):
            try:
                await self._safe_send(ui, frame)
                delivered += 1
            except Exception:
                logger.debug("agent_bundle_deliver send failed", exc_info=True)
        if delivered == 0:
            logger.warning("058: no desktop host online for owner=%s to deliver agent %s",
                           owner_sub, agent_id)
        await self._audit_user_agent(
            owner_sub, "agent.bundle_delivered",
            f"Delivered user-agent bundle to {delivered} desktop host socket(s)"
            + ("" if delivered else " — no desktop host online."),
            agent_id, outcome=("success" if delivered else "failure"))
        return delivered

    async def _deliver_personal_agent_revision(
        self,
        *,
        owner_sub: str,
        agent_id: str,
        files: Dict[str, str],
        runtime_manifest: Dict[str, Any],
        bundle_sha256: str,
        revision_id: str,
        artifact_relative_path: str,
        runtime_contract_version: int,
        required_runtime_lock_sha256: str,
        _activation_locked: bool = False,
    ) -> int:
        from orchestrator.agent_lifecycle import (
            AgentRevisionActivator,
            CandidatePreparation,
        )

        revision_id = self._strict_uuid4(revision_id, "revision_id")
        if runtime_manifest.get("revision_id") != revision_id:
            raise ValueError("runtime manifest revision identity is stale")
        if runtime_manifest.get("agent_id") != agent_id:
            raise ValueError("runtime manifest agent identity is stale")
        if (
            not isinstance(bundle_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", bundle_sha256) is None
            or runtime_manifest.get("bundle_sha256") != bundle_sha256
        ):
            raise ValueError("runtime bundle digest is invalid")
        if (
            not isinstance(artifact_relative_path, str)
            or not artifact_relative_path
        ):
            raise ValueError("immutable artifact path is required")

        # Duplicate authoring requests for the same immutable revision share one
        # activation in this process. The durable submission/revision fences are
        # still the cross-replica authority; this lock merely prevents a local
        # replay from waiting on (or trying to terminalize) its already-running
        # delivery operation.
        activation_key = (owner_sub, agent_id, revision_id)
        if not _activation_locked:
            locks = getattr(self, "_personal_agent_activation_locks", None)
            if locks is None:
                locks = self._personal_agent_activation_locks = {}
            lock = locks.setdefault(activation_key, asyncio.Lock())
            try:
                async with lock:
                    return await self._deliver_personal_agent_revision(
                        owner_sub=owner_sub,
                        agent_id=agent_id,
                        files=files,
                        runtime_manifest=runtime_manifest,
                        bundle_sha256=bundle_sha256,
                        revision_id=revision_id,
                        artifact_relative_path=artifact_relative_path,
                        runtime_contract_version=runtime_contract_version,
                        required_runtime_lock_sha256=(
                            required_runtime_lock_sha256
                        ),
                        _activation_locked=True,
                    )
            finally:
                if (
                    locks.get(activation_key) is lock
                    and not lock.locked()
                ):
                    locks.pop(activation_key, None)

        # A same-process replay after commit is already successful. Re-publish
        # only if its exact durable route projection is absent; never create a
        # second delivery/runtime generation for the active revision.
        try:
            online = await asyncio.to_thread(
                self.personal_agent_runtime.get_current_online_authority,
                owner_user_id=owner_sub,
                agent_id=agent_id,
            )
        except Exception:
            online = None
        if online is not None and online.fence.revision_id == revision_id:
            projected = self.agents.get(agent_id)
            if getattr(projected, "runtime_fence", None) != online.fence:
                await self._publish_personal_agent_runtime(online)
            return 1

        selection = await asyncio.to_thread(
            self.personal_agent_runtime.select_host_for_agent,
            owner_user_id=owner_sub,
            agent_id=agent_id,
        )
        host = selection.session
        if host is None or host.inventory_state != "reconciled":
            await self._audit_user_agent(
                owner_sub,
                "agent.bundle_delivered",
                "No reconciled selected desktop host was available.",
                agent_id,
                outcome="failure",
            )
            return 0
        websocket = (
            getattr(self, "_personal_agent_session_sockets", None) or {}
        ).get(host.host_session_id)
        if websocket is None:
            return 0

        claimed = await self._claim_personal_agent_operation(
            owner_user_id=owner_sub,
            operation_kind="agent_runtime_delivery",
            idempotency_namespace="personal_agent_revision_delivery",
            idempotency_key=f"{agent_id}:{revision_id}",
            normalized_identity={
                "agent_id": agent_id,
                "revision_id": revision_id,
                "bundle_sha256": bundle_sha256,
                "host_session_id": host.host_session_id,
            },
            wait_seconds=5.0,
        )
        if claimed is None:
            return 0
        _operation_owner, operation_claim = claimed
        preparation = CandidatePreparation(
            owner_user_id=owner_sub,
            agent_id=agent_id,
            revision_id=revision_id,
            bundle_sha256=bundle_sha256,
            runtime_manifest=runtime_manifest,
            artifact_relative_path=artifact_relative_path,
            runtime_contract_version=runtime_contract_version,
            required_runtime_lock_sha256=required_runtime_lock_sha256,
            host_session_id=host.host_session_id,
            operation_fence=operation_claim.fence,
        )
        activation_runtime_id: str | None = None

        async def _start_candidate(candidate: Any) -> str:
            nonlocal activation_runtime_id
            runtime = await asyncio.to_thread(
                self.personal_agent_runtime.get_runtime_instance,
                candidate.runtime_instance_id,
            )
            if runtime.fence.process_id is not None:
                raise RuntimeError("candidate runtime process was already bound")
            selected_socket = (
                getattr(self, "_personal_agent_session_sockets", None) or {}
            ).get(runtime.fence.host_session_id)
            if selected_socket is not websocket:
                raise RuntimeError("candidate selected host session is stale")
            loop = asyncio.get_running_loop()
            waiter = loop.create_future()
            self._personal_agent_ready_waiters[
                runtime.fence.runtime_instance_id
            ] = waiter
            activation_runtime_id = runtime.fence.runtime_instance_id
            delivered = await self._safe_send(
                selected_socket,
                json.dumps(
                    {
                        "type": "agent_bundle_deliver",
                        "fence": runtime.fence.to_dict(),
                        "runtime_contract_version": runtime_contract_version,
                        "required_runtime_lock_sha256": (
                            required_runtime_lock_sha256
                        ),
                        "bundle_sha256": bundle_sha256,
                        "files": files,
                    },
                    separators=(",", ":"),
                ),
            )
            if not delivered:
                raise RuntimeError("candidate selected-host send failed")
            await self._emit_personal_agent_lifecycle(
                owner_sub,
                runtime,
                state=(
                    "updating"
                    if candidate.previous_runtime_instance_id is not None
                    else "starting"
                ),
            )
            return runtime.fence.runtime_instance_id

        async def _await_candidate_ready(candidate: Any) -> str:
            waiter = self._personal_agent_ready_waiters.get(
                candidate.runtime_instance_id
            )
            if waiter is None:
                raise RuntimeError("candidate ready waiter is missing")
            async with asyncio.timeout(PERSONAL_AGENT_STARTUP_TIMEOUT_SECONDS):
                return await asyncio.shield(waiter)

        async def _stop_runtime(runtime_instance_id: str) -> None:
            try:
                runtime = await asyncio.to_thread(
                    self.personal_agent_runtime.get_runtime_instance,
                    runtime_instance_id,
                )
            except Exception:
                return
            if runtime.fence.process_id is not None:
                selected_socket = (
                    getattr(self, "_personal_agent_session_sockets", None) or {}
                ).get(runtime.fence.host_session_id)
                if selected_socket is not None:
                    await self._safe_send(
                        selected_socket,
                        json.dumps(
                            {"type": "agent_stop", "fence": runtime.fence.to_dict()},
                            separators=(",", ":"),
                        ),
                    )
            try:
                await self._terminalize_personal_agent_runtime(
                    runtime.fence,
                    failure_code="revision_promotion_failed",
                )
            except Exception:
                logger.debug("candidate runtime was already terminal", exc_info=True)

        activator = AgentRevisionActivator(
            store=self.personal_agent_revisions,
            start_candidate=_start_candidate,
            await_candidate_ready=_await_candidate_ready,
            stop_runtime=_stop_runtime,
        )
        try:
            activation = await activator.activate(preparation)
            online = await asyncio.to_thread(
                self.personal_agent_runtime.get_runtime_instance,
                activation.commit.runtime_instance_id,
            )
            await self._call_work_admission(
                self.work_admission.terminalize,
                operation_claim.fence,
                state=OperationState.COMPLETED,
                terminal_code=None,
                safe_summary="Personal-agent revision is online",
                retry_after_ms=None,
            )
            await self._publish_personal_agent_runtime(online)
        except Exception:
            try:
                await self._call_work_admission(
                    self.work_admission.terminalize,
                    operation_claim.fence,
                    state=OperationState.FAILED,
                    terminal_code="revision_promotion_failed",
                    safe_summary="Personal-agent revision activation failed",
                    retry_after_ms=None,
                )
            except Exception:
                logger.debug("delivery operation already terminal", exc_info=True)
            logger.warning(
                "Personal-agent revision activation failed agent=%s revision=%s",
                agent_id,
                revision_id,
                exc_info=True,
            )
            await self._audit_user_agent(
                owner_sub,
                "agent.bundle_delivered",
                "Personal-agent revision failed before durable promotion.",
                agent_id,
                outcome="failure",
            )
            return 0
        finally:
            if activation_runtime_id is not None:
                self._personal_agent_ready_waiters.pop(
                    activation_runtime_id, None
                )

        await self._audit_user_agent(
            owner_sub,
            "agent.bundle_delivered",
            "Delivered and durably promoted one immutable personal-agent revision.",
            agent_id,
            outcome="success",
        )
        return 1

    async def delete_user_agent(self, owner_sub, agent_id):
        """Commit the durable tombstone generation before any host cleanup."""
        from shared.local_transport import TunnelSocket
        from orchestrator import user_agents as _ua
        row = await asyncio.to_thread(_ua.get_user_agent, self.history.db, agent_id)
        if row is None or row.get("owner_user_id") != owner_sub:
            return False
        try:
            tombstone = await asyncio.to_thread(
                self.personal_agent_runtime.tombstone_agent,
                owner_user_id=owner_sub,
                agent_id=agent_id,
                expected_state_revision=row.get("state_revision"),
            )
        except _ua.PersonalAgentNotFoundError:
            return False

        cleanup = None
        try:
            cleanup = await asyncio.to_thread(
                self.personal_agent_runtime.cleanup_tombstoned_agent,
                tombstone,
            )
            await self._fail_personal_agent_waiters(
                cleanup.settled_request_ids,
                code="agent_deleted",
            )
        except Exception:
            # The tombstone is already authoritative and must never be rolled
            # back. A repeated delete replays the same tombstone and retries this
            # fenced cleanup; local routing is removed below in either case.
            logger.warning(
                "Personal-agent tombstone cleanup will require reconciliation",
                exc_info=True,
            )

        try:
            from orchestrator.agent_lifecycle import publish_agent_lifecycle

            await publish_agent_lifecycle(
                self,
                owner_sub,
                agent_id=agent_id,
                revision_id=None,
                runtime_instance_id=None,
                lifecycle_generation=tombstone.lifecycle_generation,
                state_revision=tombstone.state_revision,
                state="offline",
                reason_code="agent_deleted",
            )
        except Exception:
            logger.debug(
                "Personal-agent delete lifecycle projection failed",
                exc_info=True,
            )

        # Only the committed tombstone authorizes destructive projection and
        # process cleanup. Delayed registration/delivery frames now fail their
        # durable generation checks and cannot resurrect this identity.
        fenced_sockets = [
            (runtime_id, socket)
            for runtime_id, socket in list(
                (getattr(self, "_personal_agent_runtime_sockets", None) or {}).items()
            )
            if getattr(socket, "agent_id", None) == agent_id
            and getattr(socket, "owner_sub", None) == owner_sub
        ]
        legacy_socket = self._tunnel_sockets.pop((owner_sub, agent_id), None)
        projected_socket = self.agents.get(agent_id)
        if (
            isinstance(projected_socket, TunnelSocket)
            or getattr(projected_socket, "owner_sub", None) == owner_sub
        ):
            self.agents.pop(agent_id, None)
        self.agent_cards.pop(agent_id, None)
        sent_runtime_ids: set[str] = set()
        for runtime_id, socket in fenced_sockets:
            fence = socket.runtime_fence
            sent_runtime_ids.add(runtime_id)
            self._personal_agent_runtime_sockets.pop(runtime_id, None)
            if self.agents.get(agent_id) is socket:
                self.agents.pop(agent_id, None)
            try:
                await self._safe_send(
                    socket.ui_websocket,
                    json.dumps(
                        {"type": "agent_stop", "fence": fence.to_dict()},
                        separators=(",", ":"),
                    ),
                )
            except Exception:
                logger.debug("fenced agent_stop send failed", exc_info=True)
        if cleanup is not None:
            for settlement in cleanup.settlements:
                fence = settlement.instance.fence
                if (
                    fence.runtime_instance_id in sent_runtime_ids
                    or fence.process_id is None
                ):
                    continue
                host_socket = (
                    getattr(self, "_personal_agent_session_sockets", None) or {}
                ).get(fence.host_session_id)
                if host_socket is None:
                    continue
                try:
                    await self._safe_send(
                        host_socket,
                        json.dumps(
                            {"type": "agent_stop", "fence": fence.to_dict()},
                            separators=(",", ":"),
                        ),
                    )
                except Exception:
                    logger.debug("fenced agent_stop send failed", exc_info=True)
        if legacy_socket is not None:
            # Explicit feature-058 compatibility only. V2 cleanup is never
            # broadcast to unrelated owner sockets or unselected standbys.
            try:
                await self._safe_send(
                    legacy_socket.ui_websocket,
                    json.dumps({"type": "agent_stop", "agent_id": agent_id}),
                )
            except Exception:
                logger.debug("legacy agent_stop send failed", exc_info=True)
        logger.info(
            "060: tombstoned user agent %s generation=%s (owner=%s)",
            agent_id,
            tombstone.lifecycle_generation,
            owner_sub,
        )
        await self._audit_user_agent(
            owner_sub, "agent.deleted",
            "Soft-deleted user agent (row + audit retained, host stopped).",
            agent_id)
        return True

    async def register_agent(self, websocket, msg: RegisterAgent):
        """Register a specialist agent and store its capabilities."""
        card = msg.agent_card
        if not card:
            logger.warning("RegisterAgent with no card")
            return

        # 058 (BYO agents) — a user-agent TUNNEL registration is authenticated by
        # the OWNER's UI session (not the shared AGENT_API_KEY). Owner-binding is
        # the security decision: derive the owner from the authenticated socket
        # and refuse unless the registry vouches for (owner, agent_id, runnable
        # status). Non-tunnel agents (built-in loopback, external WS) keep the
        # 028 shared-key check.
        is_tunnel = bool(getattr(websocket, "is_user_agent_tunnel", False))
        is_fenced_tunnel = bool(
            getattr(websocket, "is_fenced_user_agent_tunnel", False)
        )
        if is_tunnel:
            from orchestrator.user_agents import authorize_registration
            owner_sub = getattr(websocket, "owner_sub", None)
            reserved = frozenset(getattr(self.history.db, "_FIRST_PARTY_PUBLIC_AGENT_IDS", ()) or ())
            ok, reason = await asyncio.to_thread(
                authorize_registration, self.history.db, owner_sub, card.agent_id,
                reserved_ids=reserved)
            if not ok:
                logger.warning(
                    "Refusing user-agent tunnel registration '%s' (owner=%s): %s",
                    card.agent_id, owner_sub, reason)
                # T035/FR-012: a refused boundary registration must leave an
                # audited trail (owner isolation / forged-id / reserved-id / stale
                # status), not just a log line.
                await self._audit_user_agent(
                    owner_sub, "agent.registration_refused",
                    f"Refused user-agent tunnel registration: {reason}",
                    card.agent_id, outcome="failure")
                if websocket is not None:
                    try:
                        await websocket.close(code=1008, reason="user-agent registration refused")
                    except Exception:
                        logger.debug("close after refused user-agent registration failed", exc_info=True)
                return
        else:
            # 028 FR-016 — agent connections are authenticated. In production
            # (ASTRAL_ENV != development) a missing/invalid key refuses the
            # registration outright (fail closed); dev mode stays keyless.
            from orchestrator.auth import validate_agent_api_key
            if not validate_agent_api_key(getattr(msg, "api_key", None) or ""):
                logger.warning(
                    "Refusing agent registration for '%s': missing or invalid agent "
                    "API key (028 FR-016 fail-closed)", card.agent_id)
                if websocket is not None:
                    try:
                        await websocket.close(code=1008, reason="agent authentication required")
                    except Exception:
                        logger.debug("close after refused agent registration failed", exc_info=True)
                return

        # 064 Phase A: validate the agent's exact JSON Schema 2020-12
        # declaration before publishing any card/routing state. This validator
        # is bounded and offline; it never dereferences a network URI. Only
        # after validation is an absent dialect made explicit.
        from shared.schema_validation import validate_tool_schema

        try:
            for skill in card.skills:
                skill.input_schema = validate_tool_schema(
                    skill.input_schema
                    or {"type": "object", "properties": {}},
                    skill.id or skill.name,
                    require_object_root=True,
                )
                output_schema = getattr(skill, "output_schema", None)
                if output_schema is not None:
                    skill.output_schema = validate_tool_schema(
                        output_schema,
                        skill.id or skill.name,
                        require_object_root=False,
                    )
        except ValueError as exc:
            logger.warning(
                "Refusing agent %s registration: %s",
                card.agent_id,
                exc,
            )
            if websocket is not None:
                try:
                    await websocket.close(code=1008, reason="invalid tool schema")
                except Exception:
                    logger.debug("close after invalid tool schema failed", exc_info=True)
            return

        if websocket is not None:
            self.agents[card.agent_id] = websocket
        self.agent_cards[card.agent_id] = card

        # 058: a tunnel registration is the delivered, validated user agent
        # connecting inward → go live (status='live', host session, companion
        # agent_ownership row is_public=FALSE) and record the owner-scoped socket.
        if is_tunnel:
            from orchestrator import user_agents as _ua
            if is_fenced_tunnel:
                runtime_id = websocket.runtime_fence.runtime_instance_id
                self._personal_agent_runtime_sockets[runtime_id] = websocket
            else:
                self._tunnel_sockets[
                    (getattr(websocket, "owner_sub", None), card.agent_id)
                ] = websocket
                try:
                    await asyncio.to_thread(
                        _ua.go_live, self.history.db, card.agent_id,
                        host_session_id=getattr(websocket, "host_session_id", None))
                except Exception:
                    logger.warning(
                        "go_live failed for user agent %s",
                        card.agent_id,
                        exc_info=True,
                    )
            await self._audit_user_agent(
                getattr(websocket, "owner_sub", None), "agent.went_live",
                "User agent registered inward and went live on its owner's host.",
                card.agent_id)

        # Extract capabilities for routing and tool→scope mapping
        caps = []
        tool_scope_map = {}
        for skill in card.skills:
            caps.append({
                "name": skill.id,
                "description": skill.description,
                "input_schema": skill.input_schema
            })
            # Store tool→scope mapping from agent-declared scopes. Validate the
            # declared scope so a typo'd or missing scope is surfaced rather than
            # silently inheriting the weakest kind.
            from orchestrator.tool_permissions import VALID_SCOPES as _VALID_SCOPES
            declared_scope = getattr(skill, 'scope', '') or ''
            if declared_scope and declared_scope not in _VALID_SCOPES:
                logger.warning(
                    "Agent %s tool %s declares unknown scope %r (not in VALID_SCOPES) — "
                    "it has no grantable permission surface and will be denied.",
                    card.agent_id, skill.id, declared_scope,
                )
            elif not declared_scope:
                logger.debug(
                    "Agent %s tool %s declares no scope — defaulting to tools:read.",
                    card.agent_id, skill.id,
                )
            tool_scope_map[skill.id] = declared_scope or 'tools:read'
        self.agent_capabilities[card.agent_id] = caps

        # Register tool→scope mapping in the permission manager
        self.tool_permissions.register_tool_scopes(card.agent_id, tool_scope_map)

        # Prune tool_overrides rows for tools no longer in the agent's live
        # registry. Best-effort — a transient DB error must not block agent
        # registration. Idempotent: subsequent calls find nothing to delete.
        try:
            await asyncio.to_thread(
                self.tool_permissions.cleanup_stale_tool_overrides,
                card.agent_id, list(tool_scope_map.keys())
            )
        except Exception as e:
            logger.warning(f"Stale tool_override cleanup failed for {card.agent_id}: {e}")

        # Extract streamable tool metadata for live streaming.
        # Two paths: legacy POLL streaming (orchestrator drives cadence) and
        # 001-tool-stream-ui PUSH streaming (tool is an async generator).
        for skill in card.skills:
            skill_metadata = getattr(skill, 'metadata', {}) or {}
            # Validate streaming metadata up front (001-tool-stream-ui T016).
            # Catches misconfigured tools at registration time with a clear
            # error rather than silently accepting and failing at subscribe.
            try:
                validate_streaming_metadata(skill_metadata)
            except ValueError as e:
                logger.warning(
                    f"Agent '{card.agent_id}' tool '{skill.id}' rejected: "
                    f"invalid streaming metadata: {e}"
                )
                continue

            # Legacy single-bool form: metadata.streamable is a config dict
            # (poll path). New form: metadata.streamable is True with
            # streaming_kind set to "push" or "poll".
            streamable_value = skill_metadata.get("streamable")
            if not streamable_value:
                continue
            if skill.scope not in ("tools:read", "tools:system"):
                continue

            # Determine kind: explicit metadata.streaming_kind wins; legacy
            # dict form (no kind) defaults to "poll".
            kind = skill_metadata.get("streaming_kind")
            if kind not in ("push", "poll"):
                kind = "poll"

            entry: Dict[str, Any] = {
                "agent_id": card.agent_id,
                "kind": kind,
            }
            # Poll path config
            if isinstance(streamable_value, dict):
                entry["default_interval"] = streamable_value.get("default_interval", 2)
                entry["min_interval"] = streamable_value.get("min_interval", 1)
                entry["max_interval"] = streamable_value.get("max_interval", 30)
            else:
                entry["default_interval"] = skill_metadata.get("default_interval_s", 2)
                entry["min_interval"] = 1
                entry["max_interval"] = 30
            # Push path bounds
            if kind == "push":
                entry["max_fps"] = skill_metadata.get("max_fps", 30)
                entry["min_fps"] = skill_metadata.get("min_fps", 5)
                entry["max_chunk_bytes"] = skill_metadata.get("max_chunk_bytes", 65536)
            self._streamable_tools[skill.id] = entry

        # Extract agent's ECIES public key for E2E credential encryption
        public_key_jwk = getattr(card, 'metadata', {}).get("public_key_jwk") if getattr(card, 'metadata', None) else None
        if public_key_jwk:
            self.credential_manager.register_agent_public_key(card.agent_id, public_key_jwk)
            logger.info(f"Registered ECIES public key for agent '{card.agent_id}'")

        logger.info(f"Agent registered: {card.agent_id} ({card.name}) with {len(caps)} tools")

        # Proactive security review: analyze all tools for threats
        raw_flags = self.security_analyzer.analyze_agent(card)
        if raw_flags:
            self.security_flags[card.agent_id] = {
                name: flag.to_dict() for name, flag in raw_flags.items()
            }
            logger.warning(
                f"Security review flagged {len(raw_flags)} tool(s) for agent "
                f"'{card.agent_id}': {list(raw_flags.keys())}"
            )
        else:
            self.security_flags[card.agent_id] = {}

        # Auto-assign ownership if this agent has no owner yet
        tool_names = [c["name"] for c in caps]

        def _resolve_ownership():
            """Ownership read/auto-assign off the event loop (sync DB reads)."""
            ownership = self.history.db.get_agent_ownership(card.agent_id)
            if not ownership:
                default_owner = os.environ.get("DEFAULT_AGENT_OWNER", "")
                if default_owner:
                    # Only the bundled first-party fleet is public (visible +
                    # enabled) by default. Every other ownerless registration —
                    # an external A2A agent or one discovered via
                    # A2A_EXTERNAL_AGENTS — defaults PRIVATE, i.e. off until an
                    # admin turns it on. User-created agents already carry
                    # explicit private ownership from agent_lifecycle before they
                    # register, so they never reach this branch (drafts likewise).
                    is_builtin = (
                        card.agent_id in self.history.db._FIRST_PARTY_PUBLIC_AGENT_IDS
                    )
                    self.history.db.set_agent_ownership(
                        card.agent_id, default_owner, is_public=is_builtin)
                    ownership = self.history.db.get_agent_ownership(card.agent_id) or {}
                    logger.info(
                        "Auto-assigned agent '%s' to %s (public=%s)",
                        card.agent_id, default_owner, is_builtin)
                else:
                    ownership = {}
            return ownership

        ownership = await asyncio.to_thread(_resolve_ownership)

        # Hook: AGENT_REGISTERED
        if flags.is_enabled("hook_system"):
            await self.hooks.emit(HookContext(
                event=HookEvent.AGENT_REGISTERED,
                agent_id=card.agent_id,
                metadata={"agent_name": card.name, "tool_count": len(caps)},
            ))

        # Don't broadcast draft agents to UI — they only appear in the Drafts tab
        if await asyncio.to_thread(self._is_draft_agent, card.agent_id):
            return

        # Notify UI clients (per-user scopes, tool_scope_map, security flags). For
        # a private user-agent tunnel registration, notify ONLY the owner's
        # sockets — never advertise a private agent to other users (FR-019).
        notify_targets = self.ui_clients
        if is_tunnel:
            _owner_sub = getattr(websocket, "owner_sub", None)
            notify_targets = [ui for ui in self.ui_clients
                              if self._get_user_id(ui) == _owner_sub]
        for ui in notify_targets:
            try:
                user_id = self._get_user_id(ui)
                scopes = await asyncio.to_thread(
                    self.tool_permissions.get_agent_scopes, user_id, card.agent_id)
                permissions = await asyncio.to_thread(
                    self.tool_permissions.get_effective_permissions,
                    user_id, card.agent_id, tool_names
                )
                msg = {
                    "type": "agent_registered",
                    "agent_id": card.agent_id,
                    "name": card.name,
                    "description": card.description,
                    "tools": tool_names,
                    "permissions": permissions,
                    "scopes": scopes,
                    "tool_scope_map": tool_scope_map,
                    "security_flags": self.security_flags.get(card.agent_id, {}),
                    "owner_email": ownership.get("owner_email"),
                    "is_public": bool(ownership.get("is_public", False)),
                }
                if getattr(card, 'metadata', None):
                    msg["metadata"] = card.metadata
                await self._safe_send(ui, json.dumps(msg))
            except Exception:
                pass

    async def discover_agent(self, base_url: str):
        """Discover an agent by fetching its A2A agent card and connecting via WebSocket."""
        try:
            # Fetch agent card
            card_url = f"{base_url}/.well-known/agent-card.json"
            async with aiohttp.ClientSession() as session:
                async with session.get(card_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        # Log as INFO during discovery to avoid noise during startup
                        logger.info(f"Agent card not ready yet at {card_url} (status: {resp.status})")
                        return
                    card_data = await resp.json()

            card = AgentCard.from_dict(card_data)
            agent_id = card.agent_id

            if agent_id in self.agents:
                logger.debug(f"Agent {agent_id} already connected")
                return

            # Connect via WebSocket with no size limit to allow large files
            ws_url = f"ws://{base_url.replace('http://', '').replace('https://', '')}/agent"
            ws = await websockets.connect(ws_url, max_size=50 * 1024 * 1024)

            # Listen for RegisterAgent message
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            parsed = Message.from_json(raw)
            if isinstance(parsed, RegisterAgent):
                await self.register_agent(ws, parsed)

            # Store agent URL for peer registry
            self.agent_urls[agent_id] = base_url

            # Start listening loop
            asyncio.create_task(self._agent_listen_loop(ws, agent_id))

            logger.info(f"Connected to agent: {agent_id} at {base_url}")

        except Exception as e:
            logger.debug(f"Discovery attempt to {base_url} skipped: {e}")

    async def discover_a2a_agent(self, base_url: str, notify_ui: bool = True):
        """Discover an external agent — tries WebSocket first, falls back to A2A JSON-RPC.

        Strategy:
        1. Try to connect via WebSocket (fastest, bidirectional, preferred)
        2. If WebSocket fails, fall back to official A2A protocol (JSON-RPC)
        """
        # Step 1: Try WebSocket first
        try:
            await self.discover_agent(base_url)
            # Check if WebSocket discovery succeeded
            for aid, url in self.agent_urls.items():
                if url == base_url and aid in self.agents:
                    logger.info(f"External agent at {base_url} connected via WebSocket (preferred)")
                    # Also set up A2A client as backup
                    await self._setup_a2a_client_for_agent(base_url, aid)
                    return
        except Exception as e:
            logger.debug(f"WebSocket discovery to {base_url} failed: {e}")

        # Step 2: Fall back to A2A JSON-RPC
        try:
            # C-2: never register a second, A2A-derived identity for a host that
            # a WebSocket-discovered agent already owns. The win_agent registers
            # over WS with its real id (e.g. windows-tools-1); without this guard
            # the A2A fallback would slug its display name into a phantom
            # duplicate ("windows-tools-(code-&-system)") and create stray
            # ownership/scope rows. The WS path is authoritative for these hosts.
            if base_url in self.agent_urls.values():
                logger.debug(
                    "A2A fallback skipped: an agent is already registered at %s", base_url
                )
                return
            import httpx
            from a2a.client import A2ACardResolver
            from shared.a2a_bridge import a2a_card_to_custom

            async with httpx.AsyncClient() as http_client:
                resolver = A2ACardResolver(http_client, base_url)
                a2a_card = await resolver.get_agent_card()

            custom_card = a2a_card_to_custom(a2a_card)
            agent_id = custom_card.agent_id

            if agent_id in self.agents or agent_id in self.a2a_clients:
                logger.debug(f"A2A agent {agent_id} already connected")
                return

            # Track this agent as reachable via hand-rolled JSON-RPC (v1.0 client
            # is bypassed because we POST per-call with a per-request Bearer token).
            self.a2a_clients[agent_id] = base_url
            self.a2a_agent_cards[agent_id] = a2a_card
            self.agent_urls[agent_id] = base_url

            # Internal registration of an operator-configured A2A discovery —
            # carries the orchestrator's own configured key (FR-016).
            register_msg = RegisterAgent(agent_card=custom_card,
                                         api_key=os.getenv("AGENT_API_KEY") or None)
            await self.register_agent(None, register_msg)

            logger.info(f"External agent discovered via A2A (WebSocket unavailable): {agent_id} at {base_url}")

        except Exception as e:
            logger.debug(f"A2A discovery to {base_url} also failed: {e}")

    async def _setup_a2a_client_for_agent(self, base_url: str, agent_id: str):
        """Set up an A2A backup transport for a WebSocket-connected agent.

        Records the agent's base URL so tool calls can fall back to hand-rolled
        JSON-RPC if WebSocket transport fails.
        """
        try:
            import httpx
            from a2a.client import A2ACardResolver

            a2a_url = f"{base_url}/a2a"
            async with httpx.AsyncClient() as http_client:
                resolver = A2ACardResolver(http_client, a2a_url)
                a2a_card = await resolver.get_agent_card()

            self.a2a_clients[agent_id] = base_url
            self.a2a_agent_cards[agent_id] = a2a_card
            logger.info(f"A2A backup client set up for {agent_id}")
        except Exception as e:
            logger.debug(f"A2A backup setup for {agent_id} failed (non-critical): {e}")

    async def _agent_listen_loop(self, ws, agent_id: str):
        """Listen for messages from a connected agent."""
        try:
            async for message in ws:
                await self.handle_agent_message(ws, message)
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"Agent {agent_id} disconnected")
        finally:
            if agent_id in self.agents:
                del self.agents[agent_id]
            if agent_id in self.agent_cards:
                del self.agent_cards[agent_id]
                logger.info(f"Agent {agent_id} deregistered")
            if agent_id in self.security_flags:
                del self.security_flags[agent_id]

    # =========================================================================
    # MESSAGE HANDLING
    # =========================================================================

    def _response_is_from_dispatch_target(self, req_id: str, websocket) -> bool:
        """Is this ``mcp_response`` coming from the agent we sent the request to?

        Correlation on ``request_id`` alone would let ANY connected agent resolve
        another agent's pending future. Untrusted BYO agents now share this
        router, so verify the responder: a loopback/tunnel socket carries its own
        ``agent_id``, and a networked agent socket is the object registered in
        ``self.agents`` under the dispatch target. An unrecorded request (tests,
        legacy in-process resolutions) is left alone.
        """
        expected = (getattr(self, "_pending_request_agent", None) or {}).get(req_id)
        if not expected:
            return True
        if getattr(websocket, "agent_id", None) == expected:
            return True
        return self.agents.get(expected) is websocket

    async def handle_agent_message(self, websocket, message: str):
        """Handle message from an agent."""
        try:
            msg = Message.from_json(message)

            if isinstance(msg, RegisterAgent):
                await self.register_agent(websocket, msg)

            elif isinstance(msg, MCPResponse):
                msg.validate_result_shape()
                req_id = msg.request_id
                if req_id in self.pending_requests:
                    if not self._response_is_from_dispatch_target(req_id, websocket):
                        logger.warning(
                            "Dropping mcp_response for %s: it came from a socket "
                            "that is not the agent the request was sent to (%s)",
                            req_id,
                            (getattr(self, "_pending_request_agent", None) or {}).get(req_id))
                        return
                    self.pending_requests[req_id].set_result(msg)
                else:
                    logger.warning(f"Received response for unknown request: {req_id}")

            elif isinstance(msg, AgentHopRequest):
                # 056 US1: an agent requests a MEDIATED hop to a peer tool.
                # Mediation runs as its own task so the (possibly loopback)
                # control frame returns immediately and deep hop chains never
                # nest inside this router's stack.
                task = asyncio.create_task(
                    self._handle_agent_hop_request(websocket, msg))
                self._background_hop_tasks.add(task)
                task.add_done_callback(self._background_hop_tasks.discard)

            elif isinstance(msg, ToolProgress):
                # Long-running job progress. Handled UNCONDITIONALLY — this branch
                # was previously gated behind the off-by-default progress_streaming
                # flag, which silently dropped both the auto-progress the agent
                # promised AND the concurrency-cap release.
                await self._handle_tool_progress(msg)

            # 001-tool-stream-ui: forward streaming tool chunks to subscribers
            # via StreamManager. Gated on the feature flag — when off, agents
            # never send these messages so the branches are never taken.
            elif isinstance(msg, ToolStreamData) and flags.is_enabled("tool_streaming"):
                if self.stream_manager is not None:
                    # 055 US2: capture the bridged subscription BEFORE the
                    # frame is processed — an error chunk can resolve the
                    # stream terminally, tearing the record down.
                    sub = self._bridged_stream_subscription(msg.stream_id)
                    try:
                        await self.stream_manager.handle_agent_chunk(msg)
                    except NotImplementedError:
                        # Phase 2 foundational: handlers are stubs until US1
                        # implements the routing. Drop the chunk silently
                        # while we're still building the feature.
                        logger.debug(
                            f"ToolStreamData received but stream_manager handler "
                            f"not yet implemented (stream_id={msg.stream_id})"
                        )
                    if sub is not None:
                        await self._persist_stream_terminal(sub)

            elif isinstance(msg, ToolStreamEnd) and flags.is_enabled("tool_streaming"):
                if self.stream_manager is not None:
                    sub = self._bridged_stream_subscription(msg.stream_id)
                    try:
                        await self.stream_manager.handle_agent_end(msg)
                    except NotImplementedError:
                        logger.debug(
                            f"ToolStreamEnd received but stream_manager handler "
                            f"not yet implemented (stream_id={msg.stream_id})"
                        )
                    if sub is not None:
                        await self._persist_stream_terminal(sub)

        except Exception as e:
            logger.error(f"Error handling agent message: {e}")
            # Resolve a correlatable malformed response immediately. Unknown
            # additive keys are filtered by Message.from_json; reaching this
            # branch means the known envelope itself is invalid.
            try:
                raw = json.loads(message)
            except (TypeError, json.JSONDecodeError):
                raw = None
            req_id = raw.get("request_id") if isinstance(raw, dict) else None
            if (
                isinstance(req_id, str)
                and raw.get("type") == "mcp_response"
                and req_id in self.pending_requests
                and self._response_is_from_dispatch_target(req_id, websocket)
            ):
                future = self.pending_requests[req_id]
                if not future.done():
                    future.set_exception(
                        ProtocolValidationError("malformed MCP response")
                    )

    @staticmethod
    def _parsed_ui_frame(message: str) -> dict[str, Any] | None:
        """Parse one UI frame without inferring its type from payload text."""

        try:
            value = json.loads(message)
        except (TypeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _ui_control_kind(frame: dict[str, Any] | None) -> str | None:
        """Return the structurally declared transport/control kind, if any."""

        if frame is None:
            return None
        frame_type = frame.get("type")
        if not isinstance(frame_type, str):
            return None
        if frame_type in {
            "register_ui",
            "close",
            "ping",
            "pong",
            "voice_playout_event",
        }:
            return frame_type
        if frame_type == "ui_event" and frame.get("action") == "cancel_task":
            return "cancel_task"
        if frame_type in {"cancel", "cancel_task"}:
            return "cancel_task"
        return None

    @staticmethod
    def _optional_uuid(value: Any) -> _uuid.UUID | None:
        if value in (None, ""):
            return None
        try:
            return _uuid.UUID(str(value))
        except (TypeError, ValueError, AttributeError):
            return None

    @classmethod
    def _optional_uuid4(cls, value: Any) -> _uuid.UUID | None:
        parsed = cls._optional_uuid(value)
        if (
            parsed is None
            or parsed.version != 4
            or parsed.variant != _uuid.RFC_4122
        ):
            return None
        return parsed

    @classmethod
    def _canonical_uuid4(cls, value: Any) -> _uuid.UUID | None:
        """Return only exact lowercase, hyphenated RFC 4122 UUID4 text."""

        if not isinstance(value, str):
            return None
        parsed = cls._optional_uuid4(value)
        if parsed is None or str(parsed) != value:
            return None
        return parsed

    def _new_connection_context(self, websocket: Any) -> ConnectionContext:
        contexts = getattr(self, "_connection_contexts", None)
        if contexts is None:
            contexts = {}
            self._connection_contexts = contexts
        context = ConnectionContext(
            websocket=websocket,
            connection_scope_id=_uuid.uuid4(),
            registration_deadline=(
                time.monotonic() + REGISTRATION_TIMEOUT_SECONDS
            ),
        )
        contexts[id(websocket)] = context
        return context

    def connection_diagnostics(self) -> dict[str, int]:
        """Return non-sensitive aggregate connection-runtime gauges."""

        contexts = tuple(
            getattr(self, "_connection_contexts", {}).values()
        )
        return {
            "active_connections": len(contexts),
            "tracked_tasks": sum(
                1
                for context in contexts
                for task in context.tracked_tasks
                if not task.done()
            ),
            "registration_waiters": sum(
                1
                for context in contexts
                if not context.registered and not context.closing
            ),
            "preregistration_queued": sum(
                len(context.preregistration) for context in contexts
            ),
        }

    def _capacity_event(self) -> asyncio.Event:
        event = getattr(self, "_interactive_capacity_event", None)
        if event is None:
            event = asyncio.Event()
            self._interactive_capacity_event = event
            self._interactive_capacity_revision = 0
        return event

    async def _notify_interactive_capacity(self) -> None:
        event = self._capacity_event()
        self._interactive_capacity_revision = (
            getattr(self, "_interactive_capacity_revision", 0) + 1
        )
        event.set()
        self._interactive_capacity_event = asyncio.Event()

    async def _call_work_admission(
        self,
        method: Any,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Run PostgreSQL authority calls off-loop.

        The explicitly named in-memory repository is a lock-only deterministic
        test dependency, so calling it inline avoids manufacturing hundreds of
        thread handoffs in the 1,000-frame contract probe.  Product
        construction always binds the PostgreSQL repository.
        """

        repository = getattr(self.work_admission, "_repository", None)
        if isinstance(repository, InMemoryWorkAdmissionRepository):
            return method(*args, **kwargs)
        return await asyncio.to_thread(method, *args, **kwargs)

    async def _wait_for_interactive_capacity(
        self,
        context: ConnectionContext,
        revision: int,
        *,
        allow_closing: bool = False,
    ) -> int:
        event = self._capacity_event()
        if (
            (not context.closing or allow_closing)
            and self._interactive_capacity_revision == revision
        ):
            try:
                await asyncio.wait_for(
                    event.wait(),
                    timeout=_CONNECTION_CLAIM_POLL_SECONDS,
                )
            except TimeoutError:
                pass
        return self._interactive_capacity_revision

    def _track_connection_task(
        self,
        context: ConnectionContext,
        coroutine: Any,
        *,
        name: str,
        operation: bool = False,
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine, name=name)
        context.tracked_tasks.add(task)
        if operation:
            context.operation_tasks.add(task)

        def _discard(done: asyncio.Task[Any]) -> None:
            context.tracked_tasks.discard(done)
            context.operation_tasks.discard(done)

        task.add_done_callback(_discard)
        return task

    @staticmethod
    def _rfc3339(value: datetime | None = None) -> str:
        timestamp = value or datetime.now(UTC)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")

    async def _send_admission_refusal(
        self,
        websocket: Any,
        *,
        submission_id: _uuid.UUID,
        code: str,
        retryable: bool,
        retry_after_ms: int | None = None,
    ) -> None:
        if (
            not isinstance(submission_id, _uuid.UUID)
            or submission_id.version != 4
            or submission_id.variant != _uuid.RFC_4122
        ):
            raise ValueError(
                "admission refusals require a canonical UUID4 submission_id"
            )
        observability = getattr(self, "runtime_observability", None)
        if observability is not None:
            observability.record_operation(
                "refused",
                operation_kind="connection_frame",
                result_code=code,
            )
        await self._safe_send(
            websocket,
            json.dumps(
                {
                    # Admission refusal has no operation_id and therefore is
                    # not an operation_status terminal.  Reuse the manifested
                    # error envelope and correlate the client-local submitting
                    # projection by owner-supplied submission_id.
                    "type": "error",
                    "submission_id": str(submission_id),
                    "accepted": False,
                    "code": code,
                    "message": self._admission_refusal_message(code),
                    "retryable": retryable,
                    "retry_after_ms": retry_after_ms,
                }
            ),
        )

    @staticmethod
    def _connection_admission_class(
        frame: _ConnectionIngressFrame,
    ) -> AdmissionClass:
        """Select the durable admission class for one validated UI frame."""

        if frame.operation_kind == "voice_chat_message":
            return AdmissionClass.VOICE_INTERACTIVE
        return AdmissionClass.INTERACTIVE

    async def _send_connection_admission_refusal(
        self,
        context: ConnectionContext,
        frame: _ConnectionIngressFrame,
        *,
        code: str,
        retryable: bool,
        retry_after_ms: int | None = None,
    ) -> bool:
        """Project admission refusal through the frame's canonical lifecycle.

        A validated voice-origin frame has enough immutable correlation to
        terminalize both the client and worker transcript buffers.  It must
        therefore receive ``voice_submission_rejected`` rather than the
        generic connection-operation error used by typed/UI work.  The
        transcript is still unaccepted: this path creates no message, task, or
        acknowledgement and never weakens the later proof/authorization gate.
        """

        if frame.operation_kind == "voice_chat_message":
            try:
                message = Message.from_json(frame.raw)
                if not isinstance(message, UIEvent):
                    raise ProtocolValidationError(
                        "voice admission frame must be a UI event"
                    )
                origin = message.voice_origin
            except (ProtocolValidationError, TypeError, ValueError):
                origin = None
            claims = (
                getattr(self, "ui_sessions", {}).get(context.websocket) or {}
            )
            user_id = claims.get("sub")
            if (
                origin is not None
                and isinstance(user_id, str)
                and user_id
                and frame.chat_id is not None
                and context.connection_generation is not None
            ):
                if code == "capacity_exceeded":
                    reason = "capacity_exhausted"
                    retry_policy = "explicit_user_retry"
                elif code == "idempotency_conflict":
                    reason = "invalid_binding"
                    retry_policy = "none"
                else:
                    reason = "stale_session"
                    retry_policy = "none"
                observability = getattr(
                    self, "runtime_observability", None
                )
                if observability is not None:
                    observability.record_operation(
                        "refused",
                        operation_kind=frame.operation_kind,
                        result_code=code,
                    )
                await self._reject_voice_submission(
                    context.websocket,
                    user_id=user_id,
                    origin=origin,
                    submission_id=str(frame.submission_id),
                    request_generation=str(frame.request_generation),
                    chat_id=frame.chat_id,
                    connection_generation=str(
                        context.connection_generation
                    ),
                    reason=reason,
                    retry_policy=retry_policy,
                )
                return True
        await self._send_admission_refusal(
            context.websocket,
            submission_id=frame.submission_id,
            code=code,
            retryable=retryable,
            retry_after_ms=retry_after_ms,
        )
        return False

    @staticmethod
    def _admission_refusal_message(code: str) -> str:
        """Return the non-sensitive message shared by refusal envelopes."""

        return {
            "capacity_exceeded": (
                "The request could not be accepted right now."
            ),
            "connection_closing": (
                "The connection is closing. Reconnect and try again."
            ),
            "invalid_input": "The request is invalid.",
            "registration_queue_full": (
                "Too many requests arrived before sign-in completed."
            ),
            "registration_timeout": (
                "Sign-in did not complete in time. Reconnect and try again."
            ),
        }.get(code, "The request could not be accepted.")

    async def _send_uncorrelated_error(
        self,
        websocket: Any,
        *,
        code: str,
    ) -> None:
        """Report malformed input without fabricating submission correlation."""

        observability = getattr(self, "runtime_observability", None)
        if observability is not None:
            observability.record_operation(
                "refused",
                operation_kind="connection_frame",
                result_code=code,
            )
        await self._safe_send(
            websocket,
            json.dumps(
                {
                    "type": "error",
                    "code": code,
                    "message": self._admission_refusal_message(code),
                }
            ),
        )

    async def _send_frame_refusal(
        self,
        websocket: Any,
        parsed: dict[str, Any] | None,
        *,
        code: str,
        retryable: bool,
        retry_after_ms: int | None = None,
    ) -> bool:
        """Refuse one frame, returning whether exact correlation was possible."""

        submission_id = self._client_submission_id(parsed)
        if submission_id is None:
            await self._send_uncorrelated_error(websocket, code=code)
            return False
        await self._send_admission_refusal(
            websocket,
            submission_id=submission_id,
            code=code,
            retryable=retryable,
            retry_after_ms=retry_after_ms,
        )
        return True

    async def _send_operation_accepted(
        self,
        context: ConnectionContext,
        frame: _ConnectionIngressFrame,
        admission: Any,
    ) -> None:
        observability = getattr(self, "runtime_observability", None)
        if observability is not None:
            observability.record_operation(
                "accepted",
                operation_kind=frame.operation_kind,
            )
        from orchestrator.chrome_events import emit_operation_status

        await emit_operation_status(
            self,
            context.websocket,
            operation_id=str(admission.operation_id),
            action=frame.action,
            surface=frame.surface or "operation",
            chat_id=frame.chat_id,
            connection_generation=str(context.connection_generation),
            request_generation=str(frame.request_generation),
            sequence=admission.state_revision,
            state="accepted",
            phase="accepted",
            label="Accepted",
            terminal=False,
            retryable=False,
            error=None,
            retry_after_ms=None,
            updated_at=getattr(admission, "updated_at", None),
        )

    async def _send_operation_phase(
        self,
        context: ConnectionContext,
        work: _ConnectionOperation,
        operation: Any,
        *,
        state: str,
        phase: str,
        label: str,
    ) -> None:
        subscribers = tuple(work.subscribers.values())
        if not subscribers:
            subscribers = ((context, work.frame),)
        for subscriber, frame in subscribers:
            await self._send_operation_phase_to_context(
                subscriber,
                frame,
                operation,
                state=state,
                phase=phase,
                label=label,
            )

    async def _send_operation_phase_to_context(
        self,
        context: ConnectionContext,
        frame: _ConnectionIngressFrame,
        operation: Any,
        *,
        state: str,
        phase: str,
        label: str,
    ) -> None:
        from orchestrator.chrome_events import emit_operation_status

        await emit_operation_status(
            self,
            context.websocket,
            operation_id=str(operation.operation_id),
            action=frame.action,
            surface=frame.surface or "operation",
            chat_id=frame.chat_id,
            connection_generation=str(context.connection_generation),
            request_generation=str(frame.request_generation),
            sequence=operation.state_revision,
            state=state,
            phase=phase,
            label=label,
            terminal=False,
            retryable=False,
            error=None,
            retry_after_ms=None,
            updated_at=getattr(operation, "updated_at", None),
        )

    async def _send_operation_projection(
        self,
        context: ConnectionContext,
        frame: _ConnectionIngressFrame,
        work: _ConnectionOperation,
        operation: Any,
    ) -> None:
        state = getattr(operation.state, "value", str(operation.state))
        if state in {"completed", "failed", "cancelled", "retryable"}:
            await self._send_operation_terminal_to_context(
                context, frame, work, operation
            )
            return
        phase = getattr(operation, "phase_code", None)
        if phase is None:
            await self._send_operation_accepted(context, frame, operation)
            return
        projected_state = {
            "validating_credentials": "validating",
            "saving_credentials": "persisting",
        }.get(phase, "running")
        label = {
            "validating_credentials": "Checking your provider credentials…",
            "saving_credentials": "Saving your provider settings…",
        }.get(phase, "Working…")
        await self._send_operation_phase_to_context(
            context,
            frame,
            operation,
            state=projected_state,
            phase=phase,
            label=label,
        )

    @staticmethod
    def _voice_ack_frame(
        turn: Any,
        *,
        connection_generation: str,
    ) -> dict[str, Any]:
        """Build the strict content-free acknowledgement for one message."""

        return {
            "type": "user_message_acked",
            "schema_version": "1",
            "connection_generation": connection_generation,
            "voice_turn_id": turn.turn_id,
            "submission_id": turn.submission_id,
            "request_generation": turn.request_generation,
            "chat_id": turn.chat_id,
            "message_id": int(turn.message_id),
        }

    @staticmethod
    def _voice_turn_state_frame(
        turn: Any,
        *,
        connection_generation: str,
        message: str,
    ) -> dict[str, Any]:
        """Build one content-safe lifecycle projection for a UI socket."""

        frame: dict[str, Any] = {
            "type": "voice_turn_state",
            "schema_version": "1",
            "session_id": turn.session_id,
            "connection_generation": connection_generation,
            "generation": turn.session_generation,
            "media_grant_revision": turn.media_grant_revision,
            "turn_id": turn.turn_id,
            "client_turn_id": turn.client_turn_id,
            "submission_id": turn.submission_id,
            "request_generation": turn.request_generation,
            "chat_id": turn.chat_id,
            "chat_context_revision": turn.chat_context_revision,
            "detected_language": turn.detected_language,
            "spoken_output_policy": turn.spoken_output_policy,
            "output_reason": turn.output_reason,
            "state": turn.state,
            "foreground": bool(turn.is_foreground),
            "sensitive_result_pending": bool(
                turn.state == "succeeded" and turn.sensitivity == "sensitive"
            ),
            "sequence": int(turn.announcement_sequence),
            "occurred_at": Orchestrator._voice_occurred_at(),
            "message": message,
        }
        if turn.state == "succeeded" and turn.result_commit_id:
            frame["result_id"] = turn.result_commit_id
        return frame

    async def _broadcast_voice_turn_state(
        self,
        turn: Any,
        *,
        message: str,
    ) -> None:
        """Fan a turn lifecycle notice to the user's current real UI sockets.

        Every socket receives its own current connection generation.  The
        notice is best-effort presentation of already-durable state, so a
        disconnected client can still recover the authoritative conversation
        text without causing terminal finalization to retry or duplicate.
        """

        sessions = getattr(self, "ui_sessions", {}) or {}
        contexts = getattr(self, "_connection_contexts", {}) or {}
        for websocket, claims in list(sessions.items()):
            if not isinstance(claims, dict) or claims.get("sub") != turn.user_id:
                continue
            context = contexts.get(id(websocket))
            if (
                context is None
                or not context.registered
                or context.closing
                or context.connection_generation is None
            ):
                continue
            try:
                frame = self._voice_turn_state_frame(
                    turn,
                    connection_generation=str(context.connection_generation),
                    message=message,
                )
                await self._safe_send(
                    websocket,
                    json.dumps(frame, separators=(",", ":")),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug(
                    "voice turn lifecycle notice delivery failed",
                    exc_info=True,
                )

    async def _notify_reconciled_voice_terminal_turn(self, turn: Any) -> None:
        """Project a maintenance-repaired terminal turn to current UI sockets."""

        message = {
            "succeeded": _VOICE_REQUEST_SUCCEEDED_MESSAGE,
            "cancelled": _VOICE_REQUEST_CANCELLED_MESSAGE,
            "failed": _VOICE_REQUEST_FAILED_MESSAGE,
        }.get(turn.state)
        if message is None:
            return
        await self._broadcast_voice_turn_state(turn, message=message)

    async def _send_voice_ack_to_context(
        self,
        context: ConnectionContext,
        frame: _ConnectionIngressFrame,
        turn: Any,
    ) -> bool:
        """Send an accepted voice message only to its exact retry binding."""

        if (
            turn.message_id is None
            or not self._voice_replay_frame_matches_turn(frame, turn)
        ):
            return False
        return bool(
            await self._safe_send(
                context.websocket,
                json.dumps(
                    self._voice_ack_frame(
                        turn,
                        connection_generation=str(
                            context.connection_generation
                        ),
                    ),
                    separators=(",", ":"),
                ),
            )
        )

    @staticmethod
    def _voice_replay_frame_matches_turn(
        frame: _ConnectionIngressFrame,
        turn: Any,
    ) -> bool:
        """Match the complete retained tuple before any replay disposition."""

        if frame.operation_kind != "voice_chat_message":
            return False
        payload = frame.parsed.get("payload")
        origin = payload.get("voice_origin") if isinstance(payload, dict) else None
        if not isinstance(origin, dict):
            return False
        return not (
            str(frame.submission_id) != turn.submission_id
            or str(frame.request_generation) != turn.request_generation
            or frame.chat_id != turn.chat_id
            or origin.get("session_id") != turn.session_id
            or origin.get("generation") != turn.session_generation
            or origin.get("media_grant_revision")
            != turn.media_grant_revision
            or origin.get("turn_id") != turn.turn_id
            or origin.get("client_turn_id") != turn.client_turn_id
            or origin.get("chat_context_revision")
            != turn.chat_context_revision
        )

    @staticmethod
    def _voice_turn_origin_unavailable(turn: Any) -> bool:
        return bool(
            getattr(turn, "origin_chat_unavailable_at", None) is not None
            or getattr(turn, "origin_chat_unavailable_reason", None)
            in {"deleted", "access_revoked"}
            or getattr(turn, "rejection_reason", None) == "chat_unavailable"
        )

    async def _send_voice_unavailable_replay(
        self,
        context: ConnectionContext,
        frame: _ConnectionIngressFrame,
        turn: Any,
    ) -> bool:
        """Return the retained terminal disposition without redispatch."""

        if (
            not self._voice_turn_origin_unavailable(turn)
            or not self._voice_replay_frame_matches_turn(frame, turn)
        ):
            return False
        retry_policy = (
            "none"
            if getattr(turn, "accepted_at", None) is not None
            else "explicit_user_retry"
        )
        return bool(
            await self._safe_send(
                context.websocket,
                json.dumps(
                    {
                        "type": "voice_submission_rejected",
                        "schema_version": "1",
                        "connection_generation": str(
                            context.connection_generation
                        ),
                        "session_id": turn.session_id,
                        "generation": turn.session_generation,
                        "media_grant_revision": turn.media_grant_revision,
                        "turn_id": turn.turn_id,
                        "client_turn_id": turn.client_turn_id,
                        "submission_id": turn.submission_id,
                        "request_generation": turn.request_generation,
                        "chat_id": turn.chat_id,
                        "reason": "chat_unavailable",
                        "retry_policy": retry_policy,
                        "occurred_at": self._voice_occurred_at(),
                        "message": "That conversation is no longer available.",
                    },
                    separators=(",", ":"),
                ),
            )
        )

    async def _replay_voice_ack_if_accepted(
        self,
        context: ConnectionContext,
        frame: _ConnectionIngressFrame,
    ) -> bool:
        """Reconcile a reconnect from content-free durable turn metadata."""

        if frame.operation_kind != "voice_chat_message":
            return False
        services = getattr(self, "voice_services", None)
        if services is None:
            return False
        claims = getattr(self, "ui_sessions", {}).get(context.websocket) or {}
        user_id = claims.get("sub")
        if not isinstance(user_id, str) or not user_id:
            return False
        try:
            turn = await asyncio.to_thread(
                services.repository.get_turn_by_submission,
                user_id=user_id,
                submission_id=str(frame.submission_id),
                request_generation=str(frame.request_generation),
            )
        except Exception:
            # A retry may arrive before proof verification/message acceptance;
            # the original execution remains the sole path allowed to accept.
            return False
        if self._voice_turn_origin_unavailable(turn):
            return await self._send_voice_unavailable_replay(
                context,
                frame,
                turn,
            )
        return await self._send_voice_ack_to_context(context, frame, turn)

    async def _replay_voice_unavailable_if_tombstoned(
        self,
        context: ConnectionContext,
        frame: _ConnectionIngressFrame,
    ) -> bool:
        """Suppress a process-local restart for one unavailable destination."""

        if frame.operation_kind != "voice_chat_message":
            return False
        services = getattr(self, "voice_services", None)
        claims = getattr(self, "ui_sessions", {}).get(context.websocket) or {}
        user_id = claims.get("sub")
        if services is None or not isinstance(user_id, str) or not user_id:
            return False
        try:
            turn = await asyncio.to_thread(
                services.repository.get_turn_by_submission,
                user_id=user_id,
                submission_id=str(frame.submission_id),
                request_generation=str(frame.request_generation),
            )
        except Exception:
            return False
        return await self._send_voice_unavailable_replay(
            context,
            frame,
            turn,
        )

    async def _broadcast_voice_ack(
        self,
        work: _ConnectionOperation | None,
        *,
        fallback_websocket: Any,
        fallback_connection_generation: str,
        turn: Any,
    ) -> None:
        """Acknowledge every same-operation subscriber without redispatch."""

        delivered = False
        if work is not None:
            for subscriber, frame in tuple(work.subscribers.values()):
                delivered = (
                    await self._send_voice_ack_to_context(
                        subscriber,
                        frame,
                        turn,
                    )
                    or delivered
                )
        if not delivered:
            await self._safe_send(
                fallback_websocket,
                json.dumps(
                    self._voice_ack_frame(
                        turn,
                        connection_generation=fallback_connection_generation,
                    ),
                    separators=(",", ":"),
                ),
            )

    @staticmethod
    def _public_terminal_code(code: str | None) -> str:
        if code in {"disconnect_drain_timeout", "disconnected"}:
            return "disconnected"
        if code in {
            "queue_wait_expired",
            "operation_failed",
            "cancelled_by_user",
            "deadline_exceeded",
            "validation_failed",
            "provider_unavailable",
            "network_unavailable",
            "stale_generation",
        }:
            return str(code)
        return "operation_failed"

    async def _send_operation_terminal(
        self,
        context: ConnectionContext,
        work: _ConnectionOperation,
        operation: Any,
    ) -> None:
        subscribers = tuple(work.subscribers.values())
        if not subscribers:
            subscribers = ((context, work.frame),)
        for subscriber, frame in subscribers:
            await self._send_operation_terminal_to_context(
                subscriber, frame, work, operation
            )

    async def _send_operation_terminal_to_context(
        self,
        context: ConnectionContext,
        frame: _ConnectionIngressFrame,
        work: _ConnectionOperation,
        operation: Any,
    ) -> None:
        operation_id = operation.operation_id
        if operation_id in context.terminal_emitted:
            observability = getattr(self, "runtime_observability", None)
            if observability is not None:
                observability.record_operation(
                    "duplicate_terminal_suppressed",
                    operation_kind=work.frame.operation_kind,
                )
            return
        state_value = getattr(operation.state, "value", str(operation.state))
        if state_value not in {"completed", "failed", "cancelled", "retryable"}:
            return
        context.terminal_emitted.add(operation_id)
        observability = getattr(self, "runtime_observability", None)
        if observability is not None:
            terminal_code = getattr(operation, "terminal_code", None)
            if terminal_code == "queue_wait_expired":
                observability.record_operation(
                    "queue_expired",
                    operation_kind=work.frame.operation_kind,
                    result_code="queue_wait_expired",
                )
            observability.record_operation(
                state_value,
                operation_kind=work.frame.operation_kind,
                result_code=terminal_code or state_value,
                phase=getattr(operation, "phase_code", None),
            )
            observability.record_operation(
                "terminal",
                operation_kind=work.frame.operation_kind,
                result_code=state_value,
                phase=getattr(operation, "phase_code", None),
            )
        error = None
        if state_value != "completed":
            code = self._public_terminal_code(
                getattr(operation, "terminal_code", None)
            )
            error = {
                "code": code,
                "message": (
                    "The operation was cancelled."
                    if state_value == "cancelled"
                    else {
                        "validation_failed": (
                            "Check the provider, model, and credentials, then try again."
                        ),
                        "provider_unavailable": (
                            "The provider is temporarily unavailable. Try again."
                        ),
                        "network_unavailable": (
                            "The provider could not be reached. Check the network and try again."
                        ),
                        "deadline_exceeded": (
                            "The save could not be completed in time. Try again."
                        ),
                    }.get(code, "The operation could not be completed.")
                ),
            }
        from orchestrator.chrome_events import emit_operation_status

        await emit_operation_status(
            self,
            context.websocket,
            operation_id=str(operation_id),
            action=frame.action,
            surface=frame.surface or "operation",
            chat_id=frame.chat_id,
            connection_generation=str(context.connection_generation),
            request_generation=str(frame.request_generation),
            sequence=operation.state_revision,
            state=state_value,
            phase=getattr(operation, "phase_code", None) or state_value,
            label={
                "completed": "Completed",
                "failed": "Failed",
                "cancelled": "Cancelled",
                "retryable": "Try again",
            }[state_value],
            terminal=True,
            retryable=state_value == "retryable",
            error=error,
            retry_after_ms=getattr(operation, "retry_after_ms", None),
            updated_at=getattr(operation, "updated_at", None),
        )

    async def _close_ui_socket(
        self,
        websocket: Any,
        *,
        code: int,
        reason: str,
    ) -> None:
        try:
            await websocket.close(code=code, reason=reason)
        except Exception:
            logger.debug("UI socket close failed", exc_info=True)

    async def _registration_failed(
        self,
        context: ConnectionContext,
        *,
        code: str,
        triggering_raw: str | None = None,
    ) -> None:
        queued_frames = list(context.preregistration)
        if triggering_raw is not None:
            queued_frames.append(triggering_raw)
        correlated = 0
        for queued_raw in queued_frames:
            if await self._send_frame_refusal(
                context.websocket,
                self._parsed_ui_frame(queued_raw),
                code=code,
                retryable=True,
            ):
                correlated += 1
        context.preregistration.clear()
        logger.warning(
            "UI registration refused code=%s queued_frames=%d correlated=%d",
            code,
            len(queued_frames),
            correlated,
        )
        await self._close_ui_socket(
            context.websocket,
            code=1008,
            reason=code,
        )

    def _client_submission_id(
        self, parsed: dict[str, Any] | None
    ) -> _uuid.UUID | None:
        """Recover only a valid client UUID4 for a correlated refusal.

        Validation of the rest of the event may fail, but a valid submission
        identity is still safe and necessary to terminalize the client's
        local-only ``submitting`` projection.  Invalid or absent identities
        remain uncorrelated.
        """

        frame = parsed or {}
        payload = frame.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        config = frame.get("config")
        if not isinstance(config, dict):
            config = {}
        supplied, matching = self._matching_frame_value(
            "submission_id", payload, frame, config
        )
        if not matching:
            return None
        return self._canonical_uuid4(supplied)

    @staticmethod
    def _matching_frame_value(
        field_name: str, *sources: dict[str, Any]
    ) -> tuple[Any, bool]:
        """Return one exact repeated wire value, rejecting source conflicts."""

        supplied = [
            source[field_name]
            for source in sources
            if isinstance(source, dict) and field_name in source
        ]
        if not supplied:
            return None, True
        first = supplied[0]
        return first, all(value == first for value in supplied[1:])

    def _connection_frame(
        self,
        context: ConnectionContext,
        raw: str,
        parsed: dict[str, Any] | None,
    ) -> _ConnectionIngressFrame | None:
        frame = parsed or {}
        payload = frame.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        is_ui_event = frame.get("type") == "ui_event"
        action_value = frame.get("action") or frame.get("type")
        action_is_valid = isinstance(action_value, str) and re.fullmatch(
            r"[a-z][a-z0-9_]{0,63}", action_value
        )
        if is_ui_event and not action_is_valid:
            return None
        action = action_value if action_is_valid else "connection_frame"
        config_identity = frame.get("config")
        if not isinstance(config_identity, dict):
            config_identity = {}
        supplied_submission, submission_matches = self._matching_frame_value(
            "submission_id", payload, frame, config_identity
        )
        if not submission_matches:
            return None
        submission_id = self._canonical_uuid4(supplied_submission)
        if is_ui_event and supplied_submission in (None, ""):
            return None
        if supplied_submission not in (None, "") and submission_id is None:
            return None
        submission_id = submission_id or _uuid.uuid4()
        supplied_request, request_matches = self._matching_frame_value(
            "request_generation", payload, frame, config_identity
        )
        if not request_matches:
            return None
        request_generation = self._canonical_uuid4(supplied_request)
        if is_ui_event and supplied_request in (None, ""):
            return None
        if supplied_request not in (None, "") and request_generation is None:
            return None
        request_generation = request_generation or _uuid.uuid4()
        supplied_connection, connection_matches = self._matching_frame_value(
            "connection_generation", payload, frame, config_identity
        )
        if not connection_matches:
            return None
        if supplied_connection not in (None, ""):
            connection_generation = self._canonical_uuid4(supplied_connection)
            if (
                connection_generation is None
                or (
                    context.connection_generation is not None
                    and connection_generation != context.connection_generation
                )
            ):
                return None

        surface_value, surface_matches = self._matching_frame_value(
            "surface", payload, frame
        )
        if not surface_matches:
            return None
        if surface_value is not None and (
            not isinstance(surface_value, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,127}", surface_value) is None
        ):
            return None
        surface = surface_value
        if action in _LLM_CREDENTIAL_SAVE_ACTIONS and surface is None:
            surface = "llm_settings"
        payload_chat, chat_matches = self._matching_frame_value(
            "chat_id", payload, frame
        )
        if not chat_matches:
            return None
        if payload_chat is not None and self._canonical_uuid4(payload_chat) is None:
            return None
        session_chat = frame.get("session_id")
        # Historical no-chat transport sentinels (for example ``win-client``)
        # are not conversation identities.  Ignore them rather than admitting
        # an invalid scope that would make the canonical accepted frame
        # impossible to serialize.
        chat_value = payload_chat or (
            session_chat
            if self._canonical_uuid4(session_chat) is not None
            else None
        )
        if (
            payload_chat is not None
            and self._canonical_uuid4(session_chat) is not None
            and payload_chat != session_chat
        ):
            return None
        if not chat_value and action in _CONVERSATION_MUTATION_ACTIONS:
            # Older component-action clients omitted chat_id because the
            # active canvas made it appear redundant.  Resolve it before
            # admission so the durable operation and its publication fence
            # still bind the exact conversation rather than inferring scope
            # from a later frame.
            chat_value = self._ws_active_chat.get(id(context.websocket))
        if chat_value is not None and self._canonical_uuid4(chat_value) is None:
            return None
        chat_id = str(chat_value) if chat_value is not None else None
        # Keep idempotency material non-secret.  Generic UI payloads can carry
        # chat text, PHI, credentials, or model input; none of those values may
        # be persisted even as a dictionary-attackable digest.  Submission and
        # request generations already identify the attempt, while these bounded
        # structural fields detect accidental identity reuse on the live scope.
        safe_payload_identity = {
            key: value
            for key in _CONNECTION_IDENTITY_FIELDS
            if (value := payload.get(key)) is None
            or isinstance(value, (bool, int))
            or (isinstance(value, str) and len(value) <= 512)
        }
        is_credential_save = action in _LLM_CREDENTIAL_SAVE_ACTIONS
        is_voice_chat = (
            action == "chat_message"
            and isinstance(payload.get("voice_origin"), dict)
        )
        if is_credential_save:
            # The owner-scoped submission is the durable retry identity across
            # connections.  Never hash credential/config values; a fixed
            # versioned operation identity both avoids secret-derived storage
            # and remains stable when a reconnect has a new wire generation.
            normalized = b"llm_credential_save:v1"
        elif is_voice_chat:
            origin = payload["voice_origin"]
            normalized = json.dumps(
                {
                    "operation_kind": "voice_chat_message",
                    "session_id": origin.get("session_id"),
                    "generation": origin.get("generation"),
                    "media_grant_revision": origin.get(
                        "media_grant_revision"
                    ),
                    "turn_id": origin.get("turn_id"),
                    "client_turn_id": origin.get("client_turn_id"),
                    "chat_id": chat_id,
                    "submission_id": str(submission_id),
                    "request_generation": str(request_generation),
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        else:
            normalized = json.dumps(
                {
                    "type": frame.get("type"),
                    "action": action,
                    "session_id": chat_id,
                    "surface": surface,
                    "submission_id": str(submission_id),
                    "request_generation": str(request_generation),
                    "payload_identity": safe_payload_identity,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                default=lambda _value: "unsupported",
            ).encode("utf-8")
        accepted_monotonic = time.monotonic()
        return _ConnectionIngressFrame(
            raw=raw,
            parsed=frame,
            action=action,
            surface=surface,
            chat_id=chat_id,
            submission_id=submission_id,
            request_generation=request_generation,
            normalized_digest=hashlib.sha256(normalized).hexdigest(),
            read_only=action in _READ_ONLY_UI_ACTIONS,
            operation_kind=(
                "llm_credential_save"
                if is_credential_save
                else (
                    "voice_chat_message"
                    if is_voice_chat
                    else "connection_frame"
                )
            ),
            deadline_at_monotonic=(
                accepted_monotonic + LLM_CREDENTIAL_ATTEMPT_TIMEOUT_SECONDS
                if is_credential_save
                else None
            ),
            deadline_at_utc=(
                datetime.now(UTC)
                + timedelta(seconds=LLM_CREDENTIAL_ATTEMPT_TIMEOUT_SECONDS)
                if is_credential_save
                else None
            ),
        )

    async def _enqueue_connection_frame(
        self,
        context: ConnectionContext,
        raw: str,
        parsed: dict[str, Any] | None,
    ) -> None:
        if context.closing:
            await self._send_frame_refusal(
                context.websocket,
                parsed,
                code="connection_closing",
                retryable=True,
            )
            return
        frame = self._connection_frame(context, raw, parsed)
        if frame is None:
            await self._send_frame_refusal(
                context.websocket,
                parsed,
                code="invalid_input",
                retryable=False,
            )
            return
        prior_digest = context.submission_digests.get(frame.submission_id)
        if prior_digest is not None:
            observability = getattr(self, "runtime_observability", None)
            if observability is not None:
                observability.record_operation(
                    "duplicate_submission_suppressed",
                    operation_kind="connection_frame",
                    result_code=(
                        "idempotency_conflict"
                        if prior_digest != frame.normalized_digest
                        else None
                    ),
                )
            if prior_digest != frame.normalized_digest:
                await self._send_connection_admission_refusal(
                    context,
                    frame,
                    code="idempotency_conflict",
                    retryable=False,
                )
            return
        if len(context.ingress) >= CONNECTION_INGRESS_LIMIT:
            await self._send_connection_admission_refusal(
                context,
                frame,
                code="capacity_exceeded",
                retryable=True,
                retry_after_ms=1000,
            )
            return
        context.submission_digests[
            frame.submission_id
        ] = frame.normalized_digest
        context.ingress.append(frame)
        if context.admission_task is None or context.admission_task.done():
            task = self._track_connection_task(
                context,
                self._connection_admission_pump(context),
                name=(
                    "connection-admission-"
                    f"{context.connection_scope_id}"
                ),
            )
            context.admission_task = task

    def _submit_connection_batch(
        self,
        context: ConnectionContext,
        batch: list[_ConnectionIngressFrame],
    ) -> list[tuple[_ConnectionIngressFrame, OperationOwner, Any, Any | None]]:
        coordinator = self.work_admission
        results = []
        for frame in batch:
            if frame.operation_kind in {
                "llm_credential_save",
                "voice_chat_message",
            }:
                claims = (
                    getattr(self, "ui_sessions", {}).get(context.websocket)
                    or {}
                )
                owner_user_id = claims.get("sub") or "legacy"
                owner = OperationOwner(
                    owner_scope=OwnerScope.USER,
                    owner_user_id=owner_user_id,
                    # Origin/subscriber metadata only; USER partitioning and
                    # REST authorization remain keyed by owner_user_id.
                    connection_scope_id=context.connection_scope_id,
                )
            else:
                owner = OperationOwner(
                    owner_scope=OwnerScope.CONNECTION,
                    owner_user_id=None,
                    connection_scope_id=context.connection_scope_id,
                )
            parent_value = (frame.parsed.get("payload") or {}).get(
                "parent_operation_id"
            ) if isinstance(frame.parsed.get("payload"), dict) else None
            request = OperationRequest(
                operation_kind=frame.operation_kind,
                admission_class=self._connection_admission_class(frame),
                owner=owner,
                submission_id=frame.submission_id,
                idempotency_namespace=(
                    frame.operation_kind
                    if frame.operation_kind
                    in {"llm_credential_save", "voice_chat_message"}
                    else None
                ),
                idempotency_key=(
                    str(frame.submission_id)
                    if frame.operation_kind
                    in {"llm_credential_save", "voice_chat_message"}
                    else None
                ),
                normalized_input_digest=(
                    frame.normalized_digest
                    if frame.operation_kind
                    in {"llm_credential_save", "voice_chat_message"}
                    else None
                ),
                chat_id=frame.chat_id,
                parent_operation_id=self._optional_uuid(parent_value),
                connection_generation=context.connection_generation,
                request_generation=frame.request_generation,
            )
            try:
                result = coordinator.submit(request)
            except Exception as exc:  # returned to the event loop, never exposed
                result = exc
            projection = None
            if not isinstance(result, Exception) and result.accepted:
                try:
                    projection = coordinator.query_operation(
                        owner=owner,
                        operation_id=result.operation_id,
                    )
                except Exception:
                    logger.exception(
                        "Accepted connection operation projection failed "
                        "operation_id=%s",
                        result.operation_id,
                    )
            results.append((frame, owner, result, projection))
        # ``asyncio.to_thread`` cannot kill a database call.  If disconnect
        # cancels the awaiting pump at the five-second bound, this worker still
        # owns cleanup: every accepted-but-not-yet-applied record is cancelled
        # before the thread returns, so no connection-owned operation detaches.
        if context.closing:
            for _frame, result_owner, result, _projection in results:
                if (
                    isinstance(result, Exception)
                    or not result.accepted
                    or result_owner.owner_scope is not OwnerScope.CONNECTION
                ):
                    continue
                try:
                    coordinator.cancel(
                        owner=result_owner,
                        operation_id=result.operation_id,
                        terminal_code="disconnected",
                    )
                except Exception:
                    logger.exception(
                        "Late admission cleanup failed operation_id=%s",
                        result.operation_id,
                    )
        observability = getattr(self, "runtime_observability", None)
        if observability is not None:
            observed = {
                (
                    self._connection_admission_class(frame),
                    (
                        "voice_chat_message"
                        if frame.operation_kind == "voice_chat_message"
                        else "connection_frame"
                    ),
                )
                for frame in batch
            }
            for admission_class, operation_kind in observed:
                try:
                    observability.observe_admission(
                        coordinator.inspect_admission_class(
                            admission_class
                        ),
                        operation_kind=operation_kind,
                    )
                except Exception:
                    logger.debug(
                        "%s admission metric refresh failed",
                        admission_class.value,
                        exc_info=True,
                    )
        return results

    async def _connection_admission_pump(
        self,
        context: ConnectionContext,
    ) -> None:
        # One yield coalesces an already-buffered socket burst into one off-loop
        # database handoff.  This keeps the receiver responsive and prevents
        # released slots from changing the admission result halfway through a
        # single ingress burst.
        await asyncio.sleep(0)
        while context.ingress:
            batch = list(context.ingress)
            context.ingress.clear()
            results = await self._call_work_admission(
                self._submit_connection_batch,
                context,
                batch,
            )
            scheduled: list[tuple[_ConnectionOperation, Any]] = []
            for frame, owner, result, projection in results:
                if isinstance(result, Exception):
                    context.submission_digests.pop(frame.submission_id, None)
                    logger.error(
                        "Connection admission failed",
                        exc_info=(
                            type(result),
                            result,
                            result.__traceback__,
                        ),
                    )
                    await self._send_connection_admission_refusal(
                        context,
                        frame,
                        code="operation_failed",
                        retryable=True,
                    )
                    continue
                if not result.accepted:
                    await self._send_connection_admission_refusal(
                        context,
                        frame,
                        code=result.code,
                        retryable=result.retryable,
                        retry_after_ms=result.retry_after_ms,
                    )
                    continue
                registry = getattr(self, "_reconnectable_operations", None)
                if registry is None:
                    registry = {}
                    self._reconnectable_operations = registry
                existing = (
                    registry.get(result.operation_id)
                    if owner.owner_scope is OwnerScope.USER
                    else None
                )
                if existing is not None:
                    existing.subscribers[id(context)] = (context, frame)
                    context.operations[result.operation_id] = existing
                    await self._send_operation_projection(
                        context,
                        frame,
                        existing,
                        projection or result,
                    )
                    await self._replay_voice_ack_if_accepted(
                        context,
                        frame,
                    )
                    # The first process-local worker owns execution.  A retry
                    # is only another viewer/reconciliation path.
                    continue
                captured_claims = dict(
                    getattr(self, "ui_sessions", {}).get(
                        context.websocket
                    )
                    or {}
                )
                work = _ConnectionOperation(
                    frame=frame,
                    owner=owner,
                    operation_id=result.operation_id,
                    auth_principal=(
                        captured_claims.get("preferred_username")
                        or owner.owner_user_id
                        or "unknown"
                    ),
                    auth_claims=(
                        captured_claims
                        if frame.operation_kind == "voice_chat_message"
                        else {}
                    ),
                )
                work.subscribers[id(context)] = (context, frame)
                context.operations[result.operation_id] = work
                if owner.owner_scope is OwnerScope.USER:
                    # Reserve before the first await so concurrent reconnects
                    # cannot schedule a second worker for this operation.
                    registry[result.operation_id] = work
                if (
                    projection is not None
                    and projection.state
                    in {
                        OperationState.COMPLETED,
                        OperationState.FAILED,
                        OperationState.CANCELLED,
                        OperationState.RETRYABLE,
                    }
                ):
                    await self._send_operation_projection(
                        context, frame, work, projection
                    )
                    await self._replay_voice_ack_if_accepted(
                        context,
                        frame,
                    )
                    if registry.get(result.operation_id) is work:
                        registry.pop(result.operation_id, None)
                    work.auth_claims.clear()
                    continue
                try:
                    await self._send_operation_accepted(context, frame, result)
                except Exception:
                    # No accepted operation may be stranded merely because its
                    # canonical UI projection could not be serialized.  Ingress
                    # is validated before admission, but this fail-safe owns any
                    # future validation drift: terminalize durably, discard the
                    # process-local work entry, and correlate the client's local
                    # submission without fabricating a wire operation terminal.
                    logger.exception(
                        "Accepted connection operation projection failed "
                        "operation_id=%s",
                        result.operation_id,
                    )
                    try:
                        await self._call_work_admission(
                            self.work_admission.cancel,
                            owner=owner,
                            operation_id=result.operation_id,
                            terminal_code="operation_failed",
                        )
                    except Exception:
                        logger.exception(
                            "Accepted connection operation cleanup failed "
                            "operation_id=%s",
                            result.operation_id,
                        )
                    context.operations.pop(result.operation_id, None)
                    if registry.get(result.operation_id) is work:
                        registry.pop(result.operation_id, None)
                    work.auth_claims.clear()
                    await self._send_connection_admission_refusal(
                        context,
                        frame,
                        code="operation_failed",
                        retryable=True,
                    )
                    await self._notify_interactive_capacity()
                    continue
                if await self._replay_voice_unavailable_if_tombstoned(
                    context,
                    frame,
                ):
                    # The retained owner/session/turn/submission tuple is a
                    # terminal destination tombstone. Keep the already
                    # admitted operation untouched, but never create another
                    # process-local dispatcher or tool-call path for it.
                    context.operations.pop(result.operation_id, None)
                    if registry.get(result.operation_id) is work:
                        registry.pop(result.operation_id, None)
                    work.auth_claims.clear()
                    continue
                if (
                    context.closing
                    and owner.owner_scope is OwnerScope.CONNECTION
                ):
                    terminal = await self._call_work_admission(
                        self.work_admission.cancel,
                        owner=owner,
                        operation_id=result.operation_id,
                        terminal_code="disconnected",
                    )
                    await self._send_operation_terminal(
                        context, work, terminal
                    )
                    await self._notify_interactive_capacity()
                    continue
                scheduled.append((work, projection))
            # Preserve ingress order while building a reader/writer barrier for
            # live connection state. Consecutive reads may run together, but a
            # mutation waits for every earlier read and mutation; later reads
            # in turn wait for that mutation. Transport controls never enter
            # this lane and retain their cancellation/drain bypass.
            loop = asyncio.get_running_loop()
            for work, _projection in scheduled:
                frame = work.frame
                work.lane_complete = loop.create_future()
                if frame.operation_kind == "voice_chat_message":
                    # Voice turns own durable user-scoped operation fences and
                    # private publication stages. They may overlap on one
                    # socket/chat; only the short acceptance and terminal
                    # rebase transactions serialize on the chat row.
                    work.predecessors = ()
                elif frame.read_only:
                    work.predecessors = tuple(
                        predecessor
                        for predecessor in (context.mutation_tail,)
                        if predecessor is not None
                    )
                    context.pending_reads.add(work.lane_complete)
                else:
                    predecessors = [
                        predecessor
                        for predecessor in (context.mutation_tail,)
                        if predecessor is not None
                    ]
                    predecessors.extend(context.pending_reads)
                    # A mutation closes the current reader generation. Future
                    # reads will depend on this mutation tail instead.
                    context.pending_reads.clear()
                    work.predecessors = tuple(dict.fromkeys(predecessors))
                    context.mutation_tail = work.lane_complete
                task = self._track_connection_task(
                    context,
                    self._run_connection_operation(context, work),
                    name=f"connection-operation-{work.operation_id}",
                    operation=True,
                )
                work.task = task
                if work.owner.owner_scope is OwnerScope.USER:
                    reconnectable_tasks = getattr(
                        self, "_reconnectable_operation_tasks", None
                    )
                    if reconnectable_tasks is None:
                        reconnectable_tasks = set()
                        self._reconnectable_operation_tasks = reconnectable_tasks
                    reconnectable_tasks.add(task)

                    def _release_reconnectable(
                        done: asyncio.Task[Any],
                        *,
                        accepted_work: _ConnectionOperation = work,
                    ) -> None:
                        reconnectable_tasks.discard(done)
                        active = getattr(
                            self, "_reconnectable_operations", {}
                        )
                        if active.get(accepted_work.operation_id) is accepted_work:
                            active.pop(accepted_work.operation_id, None)

                    task.add_done_callback(_release_reconnectable)

                def _release_unscheduled_lane(
                    _done: asyncio.Task[Any],
                    *,
                    accepted_work: _ConnectionOperation = work,
                ) -> None:
                    # A task cancelled before its coroutine's first scheduler
                    # turn never enters ``finally``. Release its lane future
                    # here as well so no surviving successor can deadlock.
                    completion = accepted_work.lane_complete
                    accepted_work.auth_claims.clear()
                    if completion is not None:
                        context.pending_reads.discard(completion)
                        if not completion.done():
                            completion.set_result(None)

                task.add_done_callback(_release_unscheduled_lane)
            await asyncio.sleep(0)

    async def _claim_connection_operation(
        self,
        context: ConnectionContext,
        work: _ConnectionOperation,
    ) -> tuple[Any | None, Any | None]:
        revision = getattr(self, "_interactive_capacity_revision", 0)
        while True:
            if (
                context.closing
                and work.owner.owner_scope is OwnerScope.CONNECTION
            ):
                terminal = await self._call_work_admission(
                    self.work_admission.cancel,
                    owner=work.owner,
                    operation_id=work.operation_id,
                    terminal_code="disconnected",
                )
                return None, terminal
            async with context.claim_lock:
                claim = await self._call_work_admission(
                    self.work_admission.claim_operation,
                    self._connection_admission_class(work.frame),
                    work.operation_id,
                )
            if claim is not None:
                return claim, None
            projection = await self._call_work_admission(
                self.work_admission.query_operation,
                owner=work.owner,
                operation_id=work.operation_id,
            )
            if projection.state in {
                OperationState.COMPLETED,
                OperationState.FAILED,
                OperationState.CANCELLED,
                OperationState.RETRYABLE,
            }:
                return None, projection
            revision = await self._wait_for_interactive_capacity(
                context,
                revision,
                allow_closing=(work.owner.owner_scope is OwnerScope.USER),
            )

    async def _renew_connection_lease(
        self,
        context: ConnectionContext,
        work: _ConnectionOperation,
        stop: asyncio.Event,
        worker: asyncio.Task[Any],
    ) -> None:
        interval = max(0.001, CONNECTION_LEASE_RENEW_SECONDS)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                await self._call_work_admission(
                    self.work_admission.renew_execution_lease,
                    work.fence,
                )
            except StaleExecutionFenceError:
                try:
                    projection = await self._call_work_admission(
                        self.work_admission.query_operation,
                        owner=work.owner,
                        operation_id=work.operation_id,
                    )
                except Exception:
                    projection = None
                if projection is None or projection.state not in {
                    OperationState.COMPLETED,
                    OperationState.FAILED,
                    OperationState.CANCELLED,
                    OperationState.RETRYABLE,
                }:
                    logger.warning(
                        "Interactive execution lease lost operation_id=%s",
                        work.operation_id,
                    )
                    work.lease_lost = True
                    worker.cancel()
                return
            except Exception:
                logger.exception(
                    "Interactive execution lease renewal failed operation_id=%s",
                    work.operation_id,
                )
                work.lease_lost = True
                worker.cancel()
                return

    async def _terminalize_connection_operation(
        self,
        context: ConnectionContext,
        work: _ConnectionOperation,
        *,
        state: OperationState,
        terminal_code: str | None,
        safe_summary: str,
        retry_after_ms: int | None = None,
    ) -> Any | None:
        if work.fence is None:
            try:
                if state is OperationState.RETRYABLE:
                    terminal = await self._call_work_admission(
                        self.work_admission.terminalize_unselected,
                        work.operation_id,
                        terminal_code=terminal_code or "operation_failed",
                        safe_summary=safe_summary,
                        retry_after_ms=retry_after_ms,
                    )
                    if terminal is None:
                        terminal = await self._call_work_admission(
                            self.work_admission.query_operation,
                            owner=work.owner,
                            operation_id=work.operation_id,
                        )
                else:
                    terminal = await self._call_work_admission(
                        self.work_admission.cancel,
                        owner=work.owner,
                        operation_id=work.operation_id,
                        terminal_code=terminal_code or "disconnected",
                    )
            except Exception:
                logger.debug(
                    "Unclaimed connection operation cancellation failed",
                    exc_info=True,
                )
                return None
        else:
            try:
                terminal = await self._call_work_admission(
                    self.work_admission.terminalize,
                    work.fence,
                    state=state,
                    terminal_code=terminal_code,
                    safe_summary=safe_summary,
                    retry_after_ms=retry_after_ms,
                )
            except StaleExecutionFenceError:
                try:
                    terminal = await self._call_work_admission(
                        self.work_admission.query_operation,
                        owner=work.owner,
                        operation_id=work.operation_id,
                    )
                except Exception:
                    return None
        await self._send_operation_terminal(context, work, terminal)
        await self._notify_interactive_capacity()
        return terminal

    async def _complete_connection_operation(
        self,
        context: ConnectionContext,
        work: _ConnectionOperation,
    ) -> Any | None:
        if work.frame.operation_kind != "llm_credential_save":
            return await self._terminalize_connection_operation(
                context,
                work,
                state=OperationState.COMPLETED,
                terminal_code=None,
                safe_summary="Completed",
            )
        terminal = work.committed_operation
        if terminal is None:
            terminal = await self._call_work_admission(
                self.work_admission.query_operation,
                owner=work.owner,
                operation_id=work.operation_id,
            )
        if terminal.state is not OperationState.COMPLETED:
            await self._send_operation_projection(
                context, work.frame, work, terminal
            )
            return terminal
        # Persistence already released the admission slot in the same
        # transaction as COMPLETED. Wake local queued work before best-effort
        # UI projection, which may have no surviving socket.
        await self._notify_interactive_capacity()
        deadline = work.frame.deadline_at_monotonic
        if deadline is None or time.monotonic() >= deadline:
            return terminal
        await self._send_operation_terminal(context, work, terminal)
        if time.monotonic() >= deadline:
            return terminal
        try:
            from orchestrator import llm_gate

            unlocked = await llm_gate.unlock_after_save(
                self,
                work.owner.owner_user_id or "legacy",
                coordinator=self.work_admission,
                completed_owner=work.owner,
                completed_operation_id=work.operation_id,
                deadline_at_monotonic=deadline,
            )
        except TimeoutError:
            return terminal
        except Exception:
            logger.warning(
                "Completed credential save gate projection failed",
                exc_info=True,
            )
            return terminal
        # An already-configured owner unlocks no first-run gate, so nothing
        # above closed the surface they saved from. Web's modal carries a ✕ and
        # Android has system Back, but an Apple surface is a full screen with
        # neither — leaving it up strands the user on a form whose work is
        # already committed. Mirrors the chrome handler's non-operation path.
        if not unlocked and work.frame.action == "chrome_llm_save":
            try:
                from orchestrator.chrome_events import is_native_sdui, push_close

                if is_native_sdui(self, context.websocket) and (
                    time.monotonic() < deadline
                ):
                    await push_close(self, context.websocket)
            except Exception:
                logger.debug(
                    "credential save surface close failed (non-fatal)",
                    exc_info=True,
                )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return terminal
        try:
            async with asyncio.timeout(remaining):
                await self._safe_send(
                    context.websocket,
                    json.dumps({"type": "llm_config_ack", "ok": True}),
                )
        except TimeoutError:
            pass
        return terminal

    async def _handle_llm_credential_operation(
        self,
        context: ConnectionContext,
        work: _ConnectionOperation,
    ) -> bool:
        """Run admitted Save without depending on a still-live UI session.

        The authenticated owner/principal were captured before admission.  A
        disconnect may remove ``ui_sessions[websocket]`` immediately after
        drain, so routing reconnectable work back through the connection auth
        gate would incorrectly turn an accepted Save into a no-op.  This keeps
        the same shared validation/probe/store handler while extracting only
        the already-admitted LLM surface payload.
        """

        from llm_config.ws_handlers import handle_llm_config_set

        if work.frame.action == "chrome_llm_save":
            from webrender.chrome.surfaces.llm import (
                _fields,
                _provider_key,
                _resolve_api_key,
            )

            payload = work.frame.parsed.get("payload")
            fields = _fields(payload)
            provider = _provider_key(fields)
            api_key, _used_saved = await _resolve_api_key(
                self,
                context.websocket,
                work.owner.owner_user_id or "legacy",
                fields,
            )
            config = {
                "provider": provider,
                "api_key": api_key,
                "base_url": fields.get("base_url", ""),
                "model": fields.get("model", ""),
            }
        else:
            config = work.frame.parsed.get("config")
            if not isinstance(config, dict):
                config = {}
        return await handle_llm_config_set(
            safe_send=self._safe_send,
            websocket=context.websocket,
            config=config,
            actor_user_id=work.owner.owner_user_id or "legacy",
            auth_principal=work.auth_principal or "unknown",
            store=self._llm_store,
            recorder=self.audit_recorder,
        )

    @asynccontextmanager
    async def _workspace_mutation_lock(self, chat_id: str):
        """Serialize one chat's logical canvas updates, re-entrantly per task."""

        held = _WORKSPACE_MUTATION_LOCKS.get()
        if chat_id in held:
            yield
            return
        lock = self._workspace_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            token = _WORKSPACE_MUTATION_LOCKS.set(held | {chat_id})
            try:
                yield
            finally:
                _WORKSPACE_MUTATION_LOCKS.reset(token)

    async def _conversation_mutation_chat_id(
        self,
        context: ConnectionContext,
        work: _ConnectionOperation,
    ) -> str | None:
        """Resolve and validate the exact active chat for a canvas mutation."""

        chat_id = work.frame.chat_id
        payload = work.frame.parsed.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        action = work.frame.action
        if not chat_id and action in {
            "delete_saved_component",
            "combine_components",
            "condense_components",
        }:
            candidate_ids: list[Any] = []
            if action == "delete_saved_component":
                candidate_ids = [payload.get("component_id")]
            elif action == "combine_components":
                candidate_ids = [payload.get("source_id"), payload.get("target_id")]
            else:
                values = payload.get("component_ids")
                candidate_ids = values if isinstance(values, list) else []
            for component_id in candidate_ids:
                if not isinstance(component_id, str) or not component_id:
                    continue
                row = await asyncio.to_thread(
                    self.history.get_component_by_id,
                    component_id,
                    user_id=work.owner.owner_user_id or "legacy",
                )
                if row is not None:
                    chat_id = row.get("chat_id")
                    break
        if not chat_id:
            chat_id = self._ws_active_chat.get(id(context.websocket))
        try:
            parsed = _uuid.UUID(str(chat_id))
        except (AttributeError, TypeError, ValueError):
            return None
        if parsed.version != 4 or parsed.variant != _uuid.RFC_4122:
            return None
        normalized = str(parsed)
        active = self._ws_active_chat.get(id(context.websocket))
        if active and str(active) != normalized:
            raise RuntimeError("conversation mutation does not target the active chat")
        operation_context = _CONNECTION_OPERATION_CONTEXT.get() or {}
        operation = operation_context.get("operation")
        if str(getattr(operation, "chat_id", "") or "") != normalized:
            raise RuntimeError("conversation mutation operation is not chat-scoped")
        return normalized

    async def _run_connection_ui_operation(
        self,
        context: ConnectionContext,
        work: _ConnectionOperation,
        *,
        websocket: Any | None = None,
    ) -> None:
        """Run an admitted UI frame, atomically publishing canvas mutations."""

        execution_websocket = (
            context.websocket if websocket is None else websocket
        )
        if work.frame.action not in _CONVERSATION_MUTATION_ACTIONS:
            await self.handle_ui_message(execution_websocket, work.frame.raw)
            return
        chat_id = await self._conversation_mutation_chat_id(context, work)
        if chat_id is None:
            # The existing action handler emits its normal bounded validation
            # error.  No persistence method may mutate a revisioned chat
            # without the stage established below.
            await self.handle_ui_message(execution_websocket, work.frame.raw)
            return
        user_id = work.owner.owner_user_id or "legacy"
        stage = None
        token = None
        request_generation = None
        from orchestrator.conversation_publication import (
            reset_conversation_publication,
        )

        async with self._workspace_mutation_lock(chat_id):
            try:
                stage, token, request_generation = (
                    await self._begin_conversation_publication(
                        execution_websocket,
                        chat_id=chat_id,
                        user_id=user_id,
                        operation_context=_CONNECTION_OPERATION_CONTEXT.get(),
                    )
                )
                if stage is None or request_generation is None:
                    raise RuntimeError(
                        "conversation mutation lacks publication authority"
                    )
                await self.handle_ui_message(
                    execution_websocket, work.frame.raw
                )
                if stage.dirty:
                    await self._publish_conversation_snapshot(
                        execution_websocket,
                        stage=stage,
                        request_generation=request_generation,
                    )
                else:
                    await asyncio.to_thread(
                        self.conversation_commits.abort_commit,
                        commit_id=stage.commit_id,
                        owner_user_id=stage.user_id,
                    )
                    stage.seal(committed=False)
            finally:
                if stage is not None and not stage.sealed:
                    try:
                        await asyncio.to_thread(
                            self.conversation_commits.abort_commit,
                            commit_id=stage.commit_id,
                            owner_user_id=stage.user_id,
                        )
                        stage.seal(committed=False)
                    except Exception:
                        logger.warning(
                            "conversation mutation stage abort failed",
                            exc_info=True,
                        )
                if token is not None:
                    reset_conversation_publication(token)

    async def run_detached_conversation_mutation(
        self,
        *,
        chat_id: str,
        user_id: str,
        mutation: Any,
    ) -> Any:
        """Atomically run a REST/server canvas mutation and notify live clients."""

        if not callable(mutation):
            raise TypeError("mutation must be callable")
        stage = None
        token = None
        request_generation = str(_uuid.uuid4())
        from orchestrator.conversation_publication import (
            reset_conversation_publication,
        )

        async with self._workspace_mutation_lock(chat_id):
            try:
                stage, token = await self._begin_detached_conversation_publication(
                    chat_id=chat_id,
                    user_id=user_id,
                    request_generation=request_generation,
                )
                result = await mutation()
                if not stage.dirty:
                    raise RuntimeError(
                        "conversation mutation completed without a state change"
                    )
                await self._publish_conversation_snapshot(
                    None,
                    stage=stage,
                    request_generation=request_generation,
                    server_initiated=True,
                )
                return result
            finally:
                if stage is not None and not stage.sealed:
                    try:
                        await asyncio.to_thread(
                            self.conversation_commits.abort_commit,
                            commit_id=stage.commit_id,
                            owner_user_id=stage.user_id,
                        )
                        stage.seal(committed=False)
                    except Exception:
                        logger.warning(
                            "detached conversation mutation abort failed",
                            exc_info=True,
                        )
                if token is not None:
                    reset_conversation_publication(token)

    async def _emit_long_running_operation_phase(
        self,
        context: ConnectionContext,
        work: _ConnectionOperation,
    ) -> None:
        """Durably publish a generic phase for any accepted two-second task."""

        if work.frame.operation_kind == "llm_credential_save":
            return
        await asyncio.sleep(OPERATION_PROGRESS_PHASE_SECONDS)
        terminal_states = {
            OperationState.COMPLETED,
            OperationState.FAILED,
            OperationState.CANCELLED,
            OperationState.RETRYABLE,
        }
        while work.fence is None:
            projection = await self._call_work_admission(
                self.work_admission.query_operation,
                owner=work.owner,
                operation_id=work.operation_id,
            )
            if projection.state in terminal_states:
                return
            await asyncio.sleep(0.05)
        try:
            operation = await self._call_work_admission(
                self.work_admission.update_phase,
                work.fence,
                "running",
            )
        except StaleExecutionFenceError:
            return
        await self._send_operation_phase(
            context,
            work,
            operation,
            state="running",
            phase="running",
            label="Working…",
        )

    async def _run_connection_operation(
        self,
        context: ConnectionContext,
        work: _ConnectionOperation,
    ) -> None:
        from llm_config.ws_handlers import (
            LLMConfigOperationContext,
            LLMConfigOperationFailure,
            active_llm_config_operation,
        )

        stop_renewal = asyncio.Event()
        renewal_task: asyncio.Task[Any] | None = None
        connection_operation_context: dict[str, Any] | None = None
        terminal_operation: Any = None
        progress_task = asyncio.create_task(
            self._emit_long_running_operation_phase(context, work),
            name=f"connection-progress-{work.operation_id}",
        )

        async def _execute() -> None:
            nonlocal connection_operation_context, renewal_task, terminal_operation
            # Wait before claiming an execution slot. Claiming first could
            # deadlock a small pool when a later writer occupies the only slot
            # while waiting for an earlier reader that has not yet claimed.
            if work.predecessors:
                await asyncio.gather(*work.predecessors)
            if (
                context.closing
                and work.owner.owner_scope is OwnerScope.CONNECTION
            ):
                raise asyncio.CancelledError
            claim, terminal = await self._claim_connection_operation(
                context, work
            )
            if terminal is not None:
                terminal_operation = terminal
                await self._send_operation_terminal(context, work, terminal)
                await self._notify_interactive_capacity()
                return
            if claim is None:
                return
            work.fence = claim.fence
            worker = asyncio.current_task()
            assert worker is not None
            renewal_task = asyncio.create_task(
                self._renew_connection_lease(
                    context,
                    work,
                    stop_renewal,
                    worker,
                ),
                name=f"connection-lease-{work.operation_id}",
            )
            if (
                context.closing
                and work.owner.owner_scope is OwnerScope.CONNECTION
            ):
                raise asyncio.CancelledError
            connection_operation_context = {
                "operation": claim.operation,
                "owner": work.owner,
                "execution_fence": claim.fence,
                "operation_kind": work.frame.operation_kind,
                "connection_generation": context.connection_generation,
                "request_generation": work.frame.request_generation,
            }
            token = _CONNECTION_OPERATION_CONTEXT.set(
                connection_operation_context
            )
            runtime_websocket = None
            try:
                if work.frame.operation_kind == "llm_credential_save":
                    deadline_monotonic = work.frame.deadline_at_monotonic
                    deadline_utc = work.frame.deadline_at_utc
                    if deadline_monotonic is None or deadline_utc is None:
                        raise RuntimeError(
                            "credential save is missing its attempt deadline"
                        )

                    async def _emit_phase(
                        state: str, phase: str, label: str
                    ) -> None:
                        projection = await self._call_work_admission(
                            self.work_admission.query_operation,
                            owner=work.owner,
                            operation_id=work.operation_id,
                        )
                        await self._send_operation_phase(
                            context,
                            work,
                            projection,
                            state=state,
                            phase=phase,
                            label=label,
                        )

                    async def _unlock() -> bool:
                        from orchestrator import llm_gate

                        return await llm_gate.unlock_after_save(
                            self,
                            work.owner.owner_user_id or "legacy",
                            coordinator=self.work_admission,
                            fence=claim.fence,
                            deadline_at_monotonic=deadline_monotonic,
                        )

                    llm_config_context = LLMConfigOperationContext(
                        coordinator=self.work_admission,
                        fence=claim.fence,
                        deadline_at_monotonic=deadline_monotonic,
                        deadline_at_utc=deadline_utc,
                        emit_phase=_emit_phase,
                        unlock_after_save=_unlock,
                    )
                    with active_llm_config_operation(llm_config_context):
                        await self._handle_llm_credential_operation(
                            context, work
                        )
                    if llm_config_context.failure is not None:
                        raise llm_config_context.failure
                    if llm_config_context.completed_operation is None:
                        raise RuntimeError(
                            "credential save returned without a durable terminal"
                        )
                    work.committed_operation = (
                        llm_config_context.completed_operation
                    )
                else:
                    execution_websocket = context.websocket
                    if work.frame.operation_kind == "voice_chat_message":
                        from orchestrator.async_tasks import (
                            DurableUserTurnWebSocket,
                        )

                        runtime_websocket = DurableUserTurnWebSocket(
                            context.websocket,
                            user_id=work.owner.owner_user_id or "legacy",
                        )
                        work.runtime_websocket = runtime_websocket
                        self.ui_sessions[runtime_websocket] = dict(
                            work.auth_claims
                        )
                        self.rote._profiles[runtime_websocket] = (
                            self.rote.get_profile(context.websocket)
                        )
                        execution_websocket = runtime_websocket
                    await self._run_connection_ui_operation(
                        context,
                        work,
                        websocket=execution_websocket,
                    )
            finally:
                if runtime_websocket is not None:
                    self.ui_sessions.pop(runtime_websocket, None)
                    self.rote.cleanup(runtime_websocket)
                    runtime_websocket.scrub()
                    work.runtime_websocket = None
                work.auth_claims.clear()
                _CONNECTION_OPERATION_CONTEXT.reset(token)
            voice_rejection = connection_operation_context.get(
                "voice_rejection"
            )
            voice_terminal_intent = connection_operation_context.get(
                "voice_terminal_intent"
            )
            if isinstance(voice_rejection, _VoiceOperationRejection):
                terminal_operation = await self._terminalize_connection_operation(
                    context,
                    work,
                    state=OperationState.FAILED,
                    terminal_code=voice_rejection.reason,
                    safe_summary=voice_rejection.safe_summary,
                )
            elif isinstance(
                voice_terminal_intent,
                _VoiceOperationTerminalIntent,
            ):
                terminal_operation = await self._terminalize_connection_operation(
                    context,
                    work,
                    state=voice_terminal_intent.state,
                    terminal_code=voice_terminal_intent.terminal_code,
                    safe_summary=voice_terminal_intent.safe_summary,
                    retry_after_ms=voice_terminal_intent.retry_after_ms,
                )
            else:
                terminal_operation = await self._complete_connection_operation(
                    context,
                    work,
                )

        try:
            if work.frame.operation_kind == "llm_credential_save":
                deadline = work.frame.deadline_at_monotonic
                if deadline is None:
                    raise RuntimeError(
                        "credential save is missing its attempt deadline"
                    )
                async with asyncio.timeout_at(deadline):
                    await _execute()
            else:
                await _execute()
        except LLMConfigOperationFailure as exc:
            terminal_operation = await self._terminalize_connection_operation(
                context,
                work,
                state=exc.state,
                terminal_code=exc.code,
                safe_summary=exc.safe_summary,
                retry_after_ms=exc.retry_after_ms,
            )
        except TimeoutError:
            terminal_operation = await self._terminalize_connection_operation(
                context,
                work,
                state=OperationState.RETRYABLE,
                terminal_code="deadline_exceeded",
                safe_summary="Credential save timed out",
            )
        except asyncio.CancelledError:
            terminal_operation = await self._terminalize_connection_operation(
                context,
                work,
                state=(
                    OperationState.RETRYABLE
                    if work.lease_lost
                    else OperationState.CANCELLED
                ),
                terminal_code=(
                    "stale_generation"
                    if work.lease_lost
                    else (
                        "disconnected"
                        if context.closing
                        else "cancelled_by_user"
                    )
                ),
                safe_summary=(
                    "Execution ownership changed"
                    if work.lease_lost
                    else "Cancelled"
                ),
            )
        except StaleExecutionFenceError:
            # A watchdog/lease successor may already own the first terminal.
            # Reconcile that durable winner; never replace it with late success.
            try:
                projection = await self._call_work_admission(
                    self.work_admission.query_operation,
                    owner=work.owner,
                    operation_id=work.operation_id,
                )
            except Exception:
                projection = None
            if projection is not None:
                terminal_operation = projection
                await self._send_operation_projection(
                    context, work.frame, work, projection
                )
        except Exception:
            logger.exception(
                "Connection operation failed operation_id=%s",
                work.operation_id,
            )
            terminal_operation = await self._terminalize_connection_operation(
                context,
                work,
                state=OperationState.FAILED,
                terminal_code="operation_failed",
                safe_summary="Operation failed",
            )
        finally:
            # The execution body has exited. Stop its progress and lease
            # observers before reconciliation and terminal speech; otherwise
            # a final stale renewal can cancel this runner halfway through the
            # exact-turn voice transition.
            progress_task.cancel()
            await asyncio.gather(progress_task, return_exceptions=True)
            stop_renewal.set()
            if renewal_task is not None:
                renewal_task.cancel()
                await asyncio.gather(renewal_task, return_exceptions=True)
            try:
                terminal_operation = (
                    await self._reconcile_pending_voice_operation(
                        connection_operation_context,
                        context,
                        work,
                        terminal_operation,
                    )
                )
                await self._finish_pending_voice_dispatch(
                    connection_operation_context,
                    terminal_operation,
                )
            except asyncio.CancelledError:
                logger.warning("voice_terminal_finalization_cancelled")
            except Exception:
                logger.warning(
                    "voice_terminal_finalization_unavailable",
                    exc_info=True,
                )
            # The admission-pump done callback is a second cleanup fence, but
            # the runner itself must scrub authority even when cancellation
            # lands before the inner execution context is established.
            runtime_websocket = work.runtime_websocket
            if runtime_websocket is not None:
                self.ui_sessions.pop(runtime_websocket, None)
                self.rote.cleanup(runtime_websocket)
                runtime_websocket.scrub()
                work.runtime_websocket = None
            work.auth_claims.clear()
            if work.lane_complete is not None:
                context.pending_reads.discard(work.lane_complete)
                if not work.lane_complete.done():
                    work.lane_complete.set_result(None)

    async def _run_ui_control(
        self,
        context: ConnectionContext,
        raw: str,
    ) -> None:
        try:
            await self.handle_ui_message(context.websocket, raw)
        except Exception:
            logger.exception("UI control frame failed")

    async def _run_ui_registration(
        self,
        context: ConnectionContext,
        raw: str,
    ) -> None:
        try:
            await self.handle_ui_message(context.websocket, raw)
        except Exception:
            logger.exception("UI registration frame failed")

    async def _route_ui_frame(
        self,
        context: ConnectionContext,
        raw: str,
    ) -> bool:
        parsed = self._parsed_ui_frame(raw)
        control = self._ui_control_kind(parsed)
        if control == "register_ui":
            if context.registered:
                self._track_connection_task(
                    context,
                    self._run_ui_registration(context, raw),
                    name=(
                        "connection-reregistration-"
                        f"{context.connection_scope_id}"
                    ),
                )
                return True
            remaining = context.registration_deadline - time.monotonic()
            if remaining <= 0:
                await self._registration_failed(
                    context,
                    code="registration_timeout",
                )
                return False
            registration_task = self._track_connection_task(
                context,
                self._run_ui_registration(context, raw),
                name=f"connection-registration-{context.connection_scope_id}",
            )
            registered_event = self._registered_events.get(
                id(context.websocket)
            )
            if registered_event is None:
                registered_event = asyncio.Event()
                self._registered_events[id(context.websocket)] = (
                    registered_event
                )
            event_waiter = asyncio.create_task(registered_event.wait())
            done, _pending = await asyncio.wait(
                {registration_task, event_waiter},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                registration_task.cancel()
                event_waiter.cancel()
                await asyncio.gather(
                    registration_task,
                    event_waiter,
                    return_exceptions=True,
                )
                await self._registration_failed(
                    context,
                    code="registration_timeout",
                )
                return False
            event_waiter.cancel()
            await asyncio.gather(event_waiter, return_exceptions=True)
            if (
                registered_event.is_set()
                and context.websocket in self.ui_sessions
            ):
                context.registered = True
                supplied_generation = self._optional_uuid(
                    (parsed or {}).get("connection_generation")
                )
                context.connection_generation = (
                    supplied_generation or _uuid.uuid4()
                )
                queued = list(context.preregistration)
                context.preregistration.clear()
                for queued_raw in queued:
                    await self._enqueue_connection_frame(
                        context,
                        queued_raw,
                        self._parsed_ui_frame(queued_raw),
                    )
            elif registration_task.done():
                # Invalid auth deliberately sets the legacy event so old
                # fire-and-forget waiters can recover.  The finite connection
                # scope keeps it closed until a valid retry succeeds.
                registered_event.clear()
            return True
        if control == "close":
            await self._close_ui_socket(
                context.websocket,
                code=1000,
                reason="client_close",
            )
            return False
        if control == "ping":
            await self._safe_send(
                context.websocket,
                json.dumps({"type": "pong"}),
            )
            return True
        if control == "pong":
            return True
        if control == "cancel_task":
            self._track_connection_task(
                context,
                self._run_ui_control(context, raw),
                name=(
                    f"connection-control-{control}-"
                    f"{context.connection_scope_id}"
                ),
            )
            return True
        if not context.registered:
            if len(context.preregistration) >= REGISTRATION_QUEUE_LIMIT:
                await self._registration_failed(
                    context,
                    code="registration_queue_full",
                    triggering_raw=raw,
                )
                return False
            context.preregistration.append(raw)
            return True
        if control == "voice_playout_event":
            # Playout is authenticated, content-free control evidence.  It
            # bypasses UI-action dispatch and durable operation admission.
            # Keep it inline so per-connection sequence/rate checks cannot be
            # reordered and a client cannot allocate unbounded control tasks.
            await self._run_ui_control(context, raw)
            return True
        await self._enqueue_connection_frame(context, raw, parsed)
        return True

    async def _serve_ui_frames(
        self,
        websocket: Any,
        receive: Any,
    ) -> None:
        context = self._new_connection_context(websocket)
        try:
            while not context.closing:
                if context.registered:
                    raw = await receive()
                else:
                    remaining = (
                        context.registration_deadline - time.monotonic()
                    )
                    if remaining <= 0:
                        await self._registration_failed(
                            context,
                            code="registration_timeout",
                        )
                        break
                    try:
                        raw = await asyncio.wait_for(
                            receive(), timeout=remaining
                        )
                    except TimeoutError:
                        await self._registration_failed(
                            context,
                            code="registration_timeout",
                        )
                        break
                if not await self._route_ui_frame(context, raw):
                    break
        finally:
            await self._drain_connection_context(context)

    async def _drain_connection_context(
        self,
        context: ConnectionContext,
    ) -> None:
        if context.closing:
            # The first caller owns drain; a repeated caller only waits for the
            # context to disappear from the active map.
            return
        drain_started = time.monotonic()
        context.closing = True
        context.preregistration.clear()
        context.ingress.clear()
        await self._notify_interactive_capacity()

        connection_work = tuple(
            work
            for work in context.operations.values()
            if work.owner.owner_scope is OwnerScope.CONNECTION
        )
        reconnectable_work = tuple(
            work
            for work in context.operations.values()
            if work.owner.owner_scope is not OwnerScope.CONNECTION
        )
        for work in reconnectable_work:
            # USER-owned Save continues; only this disconnected status viewer
            # is removed. Durable operation/submission GETs remain available.
            work.subscribers.pop(id(context), None)
        reconnectable_tasks = {
            work.task
            for work in reconnectable_work
            if work.task is not None and not work.task.done()
        }

        async def _request_cancel(
            work: _ConnectionOperation,
        ) -> tuple[_ConnectionOperation, Any | None]:
            try:
                terminal = await self._call_work_admission(
                    self.work_admission.cancel,
                    owner=work.owner,
                    operation_id=work.operation_id,
                    terminal_code="disconnected",
                )
            except Exception:
                logger.debug(
                    "Connection operation cancel request failed",
                    exc_info=True,
                )
                terminal = None
            return work, terminal

        cancellation_results = await asyncio.gather(
            *(
                _request_cancel(work)
                for work in connection_work
            )
        )
        for work, terminal in cancellation_results:
            if terminal is not None:
                await self._send_operation_terminal(
                    context, work, terminal
                )
        connection_operation_tasks = {
            work.task
            for work in connection_work
            if work.task is not None
        }
        for task in connection_operation_tasks:
            if not task.done():
                task.cancel()

        pending_scope = {
            task
            for task in context.tracked_tasks
            if not task.done() and task not in reconnectable_tasks
        }
        remaining = max(
            0.0,
            CONNECTION_DRAIN_TIMEOUT_SECONDS
            - (time.monotonic() - drain_started),
        )
        if pending_scope:
            _done, pending = await asyncio.wait(
                pending_scope,
                timeout=remaining,
            )
        else:
            pending = set()

        if pending:
            pending_operations = {
                work.task: work
                for work in connection_work
                if work.task in pending
            }
            async def _force_terminal(
                work: _ConnectionOperation,
            ) -> tuple[_ConnectionOperation, Any | None]:
                if work.fence is not None:
                    try:
                        terminal = await self._call_work_admission(
                            self.work_admission.terminalize,
                            work.fence,
                            state=OperationState.CANCELLED,
                            terminal_code="disconnect_drain_timeout",
                            safe_summary="Cancelled",
                            retry_after_ms=None,
                        )
                    except StaleExecutionFenceError:
                        terminal = None
                    except Exception:
                        logger.debug(
                            "Forced disconnect terminalization failed",
                            exc_info=True,
                        )
                        terminal = None
                else:
                    terminal = None
                return work, terminal

            forced_results = await asyncio.gather(
                *(
                    _force_terminal(work)
                    for work in pending_operations.values()
                )
            )
            for work, terminal in forced_results:
                if terminal is not None:
                    await self._send_operation_terminal(
                        context, work, terminal
                    )
            for task in pending_operations:
                task.cancel()
            for task in pending - set(pending_operations):
                task.cancel()
            # Give the forced cancellation a bounded scheduler turn to unwind;
            # durable fences are already terminal, so any survivor cannot
            # publish a late effect.
            for _ in range(3):
                if all(task.done() for task in pending):
                    break
                await asyncio.sleep(0)
            pending = {task for task in pending if not task.done()}
        if pending:
            logger.critical(
                "Connection drain left %d cancellation-resistant task(s) "
                "scope_id=%s",
                len(pending),
                context.connection_scope_id,
            )

        context.tracked_tasks.clear()
        context.operation_tasks.clear()
        context.operations.clear()
        context.submission_digests.clear()
        contexts = getattr(self, "_connection_contexts", {})
        if contexts.get(id(context.websocket)) is context:
            contexts.pop(id(context.websocket), None)
        observability = getattr(self, "runtime_observability", None)
        if observability is not None:
            try:
                observability.observe_disconnect_drain(
                    duration_seconds=max(
                        0.0, time.monotonic() - drain_started
                    ),
                    remainder=len(pending),
                )
            except Exception:
                logger.debug(
                    "Connection drain metric update failed",
                    exc_info=True,
                )

    async def _safe_handle_ui_message(self, websocket, message: str):
        """Compatibility wrapper using structural registration detection."""

        try:
            frame = self._parsed_ui_frame(message)
            if self._ui_control_kind(frame) != "register_ui":
                event = self._registered_events.get(id(websocket))
                if event is not None and not event.is_set():
                    await event.wait()
            await self.handle_ui_message(websocket, message)
        except Exception as exc:
            logger.error("UI message task error: %s", exc, exc_info=True)

    @staticmethod
    def _voice_occurred_at() -> str:
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace(
            "+00:00",
            "Z",
        )

    async def _handle_voice_playout_event(
        self,
        websocket: Any,
        event: VoicePlayoutEvent,
    ) -> bool:
        """Route one authenticated observation without audit or task creation."""

        session_claims = getattr(self, "ui_sessions", {}).get(websocket) or {}
        user_id = session_claims.get("sub")
        binding = getattr(self, "_voice_control_bindings", {}).get(
            id(websocket)
        )
        reason = "playout_binding_unavailable"
        if (
            not isinstance(user_id, str)
            or not user_id
            or binding is None
            or binding.subject != user_id
            or binding.device_id != event.device_id
            or binding.connection_generation != event.connection_generation
            or binding.expires_at <= datetime.now(UTC)
        ):
            logger.warning("voice_playout_event_rejected reason=%s", reason)
            return False
        device_bindings = getattr(self, "_voice_device_bindings", {})
        if device_bindings.get((user_id, event.device_id)) != id(websocket):
            logger.warning("voice_playout_event_rejected reason=%s", reason)
            return False
        services = getattr(self, "voice_services", None)
        if services is None:
            logger.warning(
                "voice_playout_event_rejected reason=voice_unavailable"
            )
            return False
        try:
            await services.handle_client_playout(
                user_id=user_id,
                claims=binding,
                event=event,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            safe_reason = getattr(exc, "code", type(exc).__name__)
            logger.warning(
                "voice_playout_event_rejected reason=%s",
                safe_reason,
            )
            return False
        return True

    async def _reject_voice_submission(
        self,
        websocket: Any,
        *,
        user_id: str,
        origin: Any,
        submission_id: str,
        request_generation: str,
        chat_id: str,
        connection_generation: str,
        reason: str,
        retry_policy: str,
    ) -> None:
        """Persist and echo one bounded pre-acceptance voice disposition."""

        services = getattr(self, "voice_services", None)
        turn = None
        worker_text_cleared = False
        if services is not None:
            try:
                mutation = await asyncio.to_thread(
                    services.repository.reject_transcript,
                    user_id=user_id,
                    turn_id=origin.turn_id,
                    reason=reason,
                    retry_policy=retry_policy,
                    now=datetime.now(UTC),
                )
                turn = mutation.turn
            except Exception:
                logger.debug(
                    "Voice transcript rejection persistence was unavailable",
                    exc_info=True,
                )
            if turn is not None:
                try:
                    await services.coordinator.emit_transcript_rejected(
                        turn,
                        reason=reason,
                        retry_policy=retry_policy,
                    )
                    worker_text_cleared = True
                except Exception:
                    logger.debug(
                        "Voice worker rejection disposition was unavailable",
                        exc_info=True,
                    )
        messages = {
            "capacity_exhausted": (
                "Voice request did not start because capacity is full. Please "
                "try again."
            ),
            "chat_unavailable": (
                "Voice request did not start because that conversation is no "
                "longer available. Choose a conversation and try again."
            ),
            "invalid_binding": (
                "Voice request did not start because it no longer matches this "
                "session. Please say it again."
            ),
            "invalid_proof": (
                "Voice request did not start because it could not be verified. "
                "Please say it again."
            ),
            "proof_expired": (
                "Voice request did not start because it expired before "
                "acceptance. Please say it again."
            ),
            "permission_denied": (
                "Voice request did not start because it is not authorized."
            ),
            "stale_session": (
                "Voice request did not start because this voice session is no "
                "longer current. Start voice again and retry."
            ),
            "malformed_final": (
                "Voice request did not start because the speech was not "
                "understood. Please say it again."
            ),
        }
        message = messages.get(
            reason,
            "Voice request did not start. Please try again.",
        )
        operation_context = _CONNECTION_OPERATION_CONTEXT.get()
        if (
            operation_context is not None
            and operation_context.get("operation_kind")
            == "voice_chat_message"
        ):
            operation_context["voice_rejection"] = _VoiceOperationRejection(
                reason=reason,
                safe_summary=message,
            )
        await self._safe_send(
            websocket,
            json.dumps(
                {
                    "type": "voice_submission_rejected",
                    "schema_version": "1",
                    "connection_generation": connection_generation,
                    "session_id": origin.session_id,
                    "generation": origin.generation,
                    "media_grant_revision": origin.media_grant_revision,
                    "turn_id": origin.turn_id,
                    "client_turn_id": origin.client_turn_id,
                    "submission_id": submission_id,
                    "request_generation": request_generation,
                    "chat_id": chat_id,
                    "reason": reason,
                    "retry_policy": retry_policy,
                    "occurred_at": self._voice_occurred_at(),
                    "message": message,
                },
                separators=(",", ":"),
            ),
        )
        guidance_scheduler = getattr(
            services,
            "schedule_preacceptance_rejection",
            None,
        )
        if (
            worker_text_cleared
            and turn is not None
            and callable(guidance_scheduler)
        ):
            try:
                guidance_scheduler(turn, reason=reason)
            except Exception:
                # A visible rejection and cleared worker transcript remain
                # terminal even if the bounded speech task cannot be started.
                logger.debug(
                    "Voice pre-acceptance guidance could not be scheduled",
                    exc_info=True,
                )

    async def _admit_voice_chat_message(
        self,
        websocket: Any,
        msg: UIEvent,
        *,
        user_id: str,
        chat_id: str,
        message: str,
    ) -> _VoiceDispatchContext | None:
        """Verify one proof-bound final before it enters ordinary chat."""

        origin = msg.voice_origin
        if origin is None:
            return None
        submission_id = str(msg.submission_id or "")
        request_generation = str(msg.request_generation or "")
        connection_generation = str(msg.connection_generation or "")
        services = getattr(self, "voice_services", None)
        if services is None:
            await self._reject_voice_submission(
                websocket,
                user_id=user_id,
                origin=origin,
                submission_id=submission_id,
                request_generation=request_generation,
                chat_id=chat_id,
                connection_generation=connection_generation,
                reason="stale_session",
                retry_policy="none",
            )
            return None
        chat = await asyncio.to_thread(
            self.history.get_chat,
            chat_id,
            user_id=user_id,
        )
        if chat is None:
            retry_policy = "explicit_user_retry"
            try:
                retained_turn = await asyncio.to_thread(
                    services.repository.get_turn_by_submission,
                    user_id=user_id,
                    submission_id=submission_id,
                    request_generation=request_generation,
                )
                if retained_turn.accepted_at is not None:
                    retry_policy = "none"
            except Exception:
                pass
            await self._reject_voice_submission(
                websocket,
                user_id=user_id,
                origin=origin,
                submission_id=submission_id,
                request_generation=request_generation,
                chat_id=chat_id,
                connection_generation=connection_generation,
                reason="chat_unavailable",
                retry_policy=retry_policy,
            )
            return None
        from orchestrator.voice_sessions import (
            TranscriptSubmission,
            TranscriptSubmissionRejected,
        )

        try:
            request = TranscriptSubmission(
                user_id=user_id,
                session_id=origin.session_id,
                generation=origin.generation,
                media_grant_revision=origin.media_grant_revision,
                turn_id=origin.turn_id,
                client_turn_id=origin.client_turn_id,
                submission_id=submission_id,
                request_generation=request_generation,
                chat_id=chat_id,
                chat_context_revision=origin.chat_context_revision,
                source_participant_identity=(
                    origin.source_participant_identity
                ),
                detected_language=origin.detected_language,
                text=message,
                text_digest_sha256=origin.text_digest_sha256,
                transcript_proof=origin.transcript_proof,
                proof_expires_at=origin.proof_expires_at,
            )
            admission = await services.admit_transcript(
                request,
                now=datetime.now(UTC),
            )
        except TranscriptSubmissionRejected as exc:
            await self._reject_voice_submission(
                websocket,
                user_id=user_id,
                origin=origin,
                submission_id=submission_id,
                request_generation=request_generation,
                chat_id=chat_id,
                connection_generation=connection_generation,
                reason=exc.reason,
                retry_policy=exc.retry_policy,
            )
            return None
        except (TypeError, ValueError):
            await self._reject_voice_submission(
                websocket,
                user_id=user_id,
                origin=origin,
                submission_id=submission_id,
                request_generation=request_generation,
                chat_id=chat_id,
                connection_generation=connection_generation,
                reason="malformed_final",
                retry_policy="explicit_user_retry",
            )
            return None
        except Exception:
            logger.warning(
                "Voice transcript admission was unavailable",
                exc_info=True,
            )
            await self._reject_voice_submission(
                websocket,
                user_id=user_id,
                origin=origin,
                submission_id=submission_id,
                request_generation=request_generation,
                chat_id=chat_id,
                connection_generation=connection_generation,
                reason="stale_session",
                retry_policy="none",
            )
            return None
        return _VoiceDispatchContext(
            admission=admission,
            connection_generation=connection_generation,
            origin=origin,
        )

    async def handle_ui_message(self, websocket, message: str):
        """Handle message from a UI client."""
        raw_frame: dict[str, Any] | None = None
        try:
            raw_frame = self._parsed_ui_frame(message)
            if (
                raw_frame is not None
                and (
                    raw_frame.get("type") in _PERSONAL_AGENT_HOST_FRAME_TYPES
                    or (
                        raw_frame.get("type") == "mcp_response"
                        and isinstance(raw_frame.get("fence"), dict)
                    )
                )
            ):
                await self._handle_personal_agent_host_frame(websocket, raw_frame)
                return
            invalid_host_field: str | None = None
            try:
                msg = Message.from_json(message)
            except ProtocolValidationError:
                # Authenticate/register the UI normally, but refuse only its
                # malformed optional host capability with the exact safe v2
                # envelope. This preserves the existing non-disclosing auth
                # path while ensuring malformed host data never gains a session.
                if not (
                    isinstance(raw_frame, dict)
                    and raw_frame.get("type") == "register_ui"
                    and isinstance(raw_frame.get("agent_host"), dict)
                ):
                    raise
                invalid_host_field = self._invalid_host_registration_field(
                    raw_frame["agent_host"]
                )
                sanitized = dict(raw_frame)
                sanitized["agent_host"] = False
                sanitized.pop("host_session_id", None)
                msg = Message.from_json(json.dumps(sanitized))

            if isinstance(msg, RegisterUI):
                token = msg.token
                user_data = None

                # Check for token validation (skip if not configured or in debug/dev mode if desired, but we want security)
                if token:
                    with perf_span("register_ui.validate"):
                        user_data = await self.validate_token(token)

                if user_data:
                    logger.info(f"UI registered: {user_data.get('preferred_username', 'unknown')}")
                    user_data["_raw_token"] = token  # Store raw token for RFC 8693 delegation
                    self.ui_sessions[websocket] = user_data
                    # A structured v2 advertisement is validated against the
                    # packaged runtime contract and receives a server-owned host
                    # session before it becomes eligible. The legacy boolean is
                    # retained only for feature-058 compatibility tests/clients;
                    # it never participates in v2 selection or delivery.
                    _hosts = getattr(self, "_agent_host_sockets", None)
                    if _hosts is not None:
                        if invalid_host_field is not None:
                            await self._refuse_personal_agent_host(
                                websocket,
                                code="invalid_host_registration",
                                details={"field": invalid_host_field},
                            )
                        elif isinstance(msg.agent_host, AgentHostRegistration):
                            await self._register_personal_agent_host(
                                websocket,
                                owner_user_id=user_data.get("sub", "legacy"),
                                registration=msg.agent_host,
                            )
                        elif (
                            msg.agent_host is True
                            or "agent_host"
                            in (getattr(msg, "capabilities", None) or [])
                        ):
                            _hosts[id(websocket)] = str(
                                getattr(msg, "host_session_id", "") or "")
                        else:
                            _hosts.pop(id(websocket), None)
                    _register_started = time.monotonic()
                    user_id = user_data.get("sub", "legacy")
                    _resume_requested = getattr(msg, "resume", None) is not None
                    _resume_confirmed_not_found = False

                    # Feature 065: voice mutations require a fresh bearer
                    # scoped to this authenticated subject, stable device, and
                    # fenced connection. Failure disables voice for this
                    # socket without weakening ordinary typed chat.
                    if msg.device_id is not None:
                        try:
                            await self._issue_voice_control_binding(
                                websocket,
                                msg,
                                user_data,
                            )
                        except VoiceControlBindingError as exc:
                            logger.warning(
                                "voice control binding unavailable: %s",
                                exc.code,
                            )

                    # Feature 052 (FR-012): profile save + login audit events
                    # leave the first-paint critical path. One task keeps the
                    # two auth events in order (the recorder then serializes
                    # per user), and the profile upsert runs off-loop.
                    asyncio.create_task(
                        asyncio.to_thread(self._save_user_profile, user_data))

                    async def _record_register_audit(claims=user_data, m=msg):
                        """Emit ws_register then the 016 entry-point action, in order."""
                        try:
                            from audit.hooks import record_auth_event
                            await record_auth_event(
                                claims=claims,
                                action="ws_register",
                                description=f"WebSocket session established for {claims.get('preferred_username', claims.get('sub', 'unknown'))}",
                            )
                            resumed_flag = bool(getattr(m, "resumed", False))
                            action = "session_resumed" if resumed_flag else "login_interactive"
                            await record_auth_event(
                                claims={**claims, "_pl_resumed": resumed_flag},
                                action=action,
                                description=(
                                    "Silent session resumed from stored credential"
                                    if resumed_flag
                                    else "Interactive login completed; new session established"
                                ),
                            )
                        except Exception as _e:
                            logger.debug(f"register audit records failed: {_e}")

                    asyncio.create_task(_record_register_audit())

                    # Feature 052 (FR-012): the handshake's independent reads
                    # run concurrently while the frames below go out.
                    _prefs_task = asyncio.create_task(asyncio.to_thread(
                        self.history.db.get_user_preferences, user_id))
                    _tools_task = asyncio.create_task(asyncio.to_thread(
                        self.compute_tools_available_for_user, user_id))

                    # Feature 054: register_ui.llm_config is accepted-and-
                    # ignored (wire compatibility with pre-054 clients). The
                    # server-persisted user_llm_config record is authoritative;
                    # the gate predicate below reads it directly.
                    if getattr(msg, "llm_config", None):
                        logger.debug(
                            "register_ui.llm_config ignored (054: server "
                            "persistence is authoritative)")
                    # The mandatory first-run gate needs the predicate before
                    # the welcome render; resolve it concurrently with the
                    # other handshake reads.
                    _llm_gate_task = asyncio.create_task(
                        self.llm_configured_for(user_data.get("sub", "")))

                    # ROTE: register device capabilities and send profile back
                    device_info = msg.device or {}
                    rote_profile = self.rote.register_device(websocket, device_info)
                    await self._safe_send(websocket, json.dumps({
                        "type": "rote_config",
                        "device_profile": rote_profile.to_dict(),
                        "speech_server_available": bool(os.getenv("SPEACHES_URL", "").strip()),
                    }))

                    # Feature 042: native SDUI clients (Windows/Android) render
                    # their top bar + settings menu from the single server-owned
                    # chrome model. Push it right after the handshake (the web
                    # shell already renders the SAME model server-side, so it
                    # neither needs nor receives this frame — Constitution XII).
                    try:
                        _dt = getattr(rote_profile.device_type, "value", str(rote_profile.device_type))
                        # Feature 051: iOS/macOS are chrome-model natives too
                        # (the watch stays chrome-free by design).
                        if _dt in ("windows", "android", "ios", "macos"):
                            from shared.protocol import ChromeMenu
                            from webrender.chrome.menu_model import menu_model_dict
                            _roles = list((user_data.get("realm_access") or {}).get("roles") or [])
                            for _c in (user_data.get("resource_access") or {}).values():
                                _roles.extend((_c or {}).get("roles") or [])
                            # Native clients: ADMIN TOOLS is web-only, and
                            # "Take the tour" is web-only (feature 043).
                            await self._safe_send(
                                websocket,
                                ChromeMenu(model=menu_model_dict(
                                    _roles, include_admin=False, include_tour=False)).to_json(),
                            )
                    except Exception as _e:  # pragma: no cover — non-fatal push
                        logger.debug(f"chrome_menu push failed (non-fatal): {_e}")

                    # Dashboard build starts only after rote_config is on the
                    # wire so clients keep seeing their device profile first.
                    _dash_task = asyncio.create_task(self.send_dashboard(websocket))

                    # Send stored user preferences (theme, etc.)
                    with perf_span("register_ui.reads", user=user_id):
                        try:
                            prefs = await _prefs_task
                            if prefs:
                                await self._safe_send(websocket, json.dumps({
                                    "type": "user_preferences",
                                    "preferences": prefs,
                                }))
                        except Exception as e:
                            logger.warning(f"Failed to load user preferences: {e}")

                        try:
                            _tools_avail = await _tools_task
                        except Exception:
                            _tools_avail = True

                    # Feature 060: resolve a fenced account-scoped locator
                    # before registration opens its queued work or welcome can
                    # paint. A transient failure preserves the locator and
                    # suppresses welcome; an owner-scoped miss uses the same
                    # non-disclosing not-found result as an explicit load.
                    if _resume_requested:
                        resume = msg.resume or {}
                        resume_chat_id = resume["active_chat_id"]
                        resume_request_generation = resume["request_generation"]
                        try:
                            await self._emit_hydration_snapshot(
                                websocket,
                                chat_id=resume_chat_id,
                                user_id=user_id,
                                connection_generation=msg.connection_generation,
                                request_generation=resume_request_generation,
                            )
                            self._ws_welcome.pop(id(websocket), None)
                            await self._resume_chat_streams(
                                websocket, user_id, resume_chat_id
                            )
                            if flags.is_enabled("bg_continuity"):
                                await self._replay_chat_task(
                                    websocket, resume_chat_id
                                )
                        except ConversationNotFound:
                            _resume_confirmed_not_found = True
                            await self._safe_send(
                                websocket,
                                json.dumps(
                                    {
                                        "type": "error",
                                        "code": "chat_not_found",
                                        "message": "Chat not found",
                                        "chat_id": resume_chat_id,
                                        "connection_generation": msg.connection_generation,
                                        "request_generation": resume_request_generation,
                                    }
                                ),
                            )
                        except Exception:
                            logger.warning(
                                "register_ui conversation hydration failed",
                                exc_info=True,
                            )
                            await self._safe_send(
                                websocket,
                                json.dumps(
                                    {
                                        "type": "error",
                                        "code": "snapshot_retryable",
                                        "message": "Conversation restore is temporarily unavailable",
                                        "chat_id": resume_chat_id,
                                        "connection_generation": msg.connection_generation,
                                        "request_generation": resume_request_generation,
                                        "retryable": True,
                                    }
                                ),
                            )

                    # Mark registration complete only after the requested
                    # locator has been owner-validated and resolved.
                    evt = self._registered_events.get(id(websocket))
                    if evt:
                        evt.set()

                    # Feature 065: publish the server-owned composer only
                    # after resume ownership has been resolved. A bounded
                    # refresh catches the worker becoming ready shortly after
                    # backend startup without making typed chat wait.
                    if (
                        msg.device_id is not None
                        and msg.connection_generation is not None
                        and "voice" in (msg.capabilities or [])
                    ):
                        self._start_voice_composer_refresh(
                            user_id=user_id,
                            device_id=msg.device_id,
                            connection_generation=msg.connection_generation,
                            selected_chat_id=(
                                (msg.resume or {}).get("active_chat_id")
                                or self._ws_active_chat.get(id(websocket))
                            ),
                        )

                    # Feature 054: mandatory first-run provider-setup gate.
                    # An unconfigured user's very first post-login surface is
                    # the setup dialog — pushed HERE, before the welcome
                    # render, and the welcome is suppressed until setup
                    # completes (spec FR-013/FR-016). The push itself is
                    # behind the FF_LLM_FIRST_RUN kill switch; the server-
                    # side REFUSALS (chat pre-flight, chrome gate) are
                    # structural and remain with the flag off. The watch is
                    # excluded by design (chrome-free) — it gets spoken
                    # guidance on AI use instead (FR-017).
                    _llm_gated = False
                    try:
                        _llm_configured = await _llm_gate_task
                    except Exception:  # pragma: no cover — fail open to welcome
                        logger.warning("llm gate predicate failed", exc_info=True)
                        _llm_configured = True
                    if not _llm_configured and self._ff_llm_first_run:
                        try:
                            _dt = getattr(
                                rote_profile.device_type, "value",
                                str(rote_profile.device_type))
                            if _dt != "watch":
                                from orchestrator import llm_gate
                                await llm_gate.push_setup_dialog(
                                    self, websocket, user_id)
                                _llm_gated = True
                        except Exception:  # non-fatal — refusals still gate
                            logger.warning(
                                "first-run LLM dialog push failed (refusals "
                                "still enforce the gate)", exc_info=True)

                    # 055 bg-continuity: a reconnecting client that carries its
                    # active chat id (RegisterUI.session_id) resumes that
                    # chat's server context without waiting for a load_chat —
                    # active-chat marker (which also suppresses the welcome
                    # canvas below), stream subscriptions, and a replay of any
                    # in-flight background task. Ownership-validated; an
                    # invalid/foreign id is ignored silently (register still
                    # succeeds).
                    if (
                        not _resume_requested
                        and msg.session_id
                        and flags.is_enabled("bg_continuity")
                    ):
                        try:
                            owned = await self.history.db.afetch_one(
                                "SELECT id FROM chats WHERE id = ? AND user_id = ?",
                                (msg.session_id, user_id))
                            if owned:
                                self._ws_active_chat[id(websocket)] = msg.session_id
                                await self._resume_chat_streams(
                                    websocket, user_id, msg.session_id)
                                await self._replay_chat_task(websocket, msg.session_id)
                        except Exception:
                            logger.debug("register_ui chat resume failed (non-fatal)",
                                         exc_info=True)

                    # Initial canvas: server-driven welcome examples when this
                    # socket has no chat to resume — ordinary astralprims
                    # components over the normal ui_render path (Constitution
                    # II: ROTE adapts them per device; nothing client-specific).
                    try:
                        if (
                            not _llm_gated
                            and not self._ws_active_chat.get(id(websocket))
                            and (
                                not _resume_requested
                                or _resume_confirmed_not_found
                            )
                        ):
                            from orchestrator.welcome import welcome_components
                            # Feature 030: tell the welcome canvas whether any
                            # tools are dispatchable so it can lead with the
                            # enable-agents consent card instead of promising
                            # examples that would silently degrade to text.
                            # speak=False: chrome, not a conversation turn —
                            # a watch must not narrate the welcome canvas on
                            # every (re)connect.
                            with perf_span("welcome.render", user=user_id):
                                await self.send_ui_render(
                                    websocket, welcome_components(tools_available=_tools_avail),
                                    speak=False)
                            self._ws_welcome[id(websocket)] = True
                    except Exception as _e:  # non-fatal — an empty canvas is fine
                        logger.debug(f"welcome canvas render failed (non-fatal): {_e}")

                    try:
                        await _dash_task
                    except Exception:
                        logger.warning("register_ui dashboard delivery failed", exc_info=True)

                    # 055 bg-continuity late-connect catch-up: in-flight tasks
                    # replay as task_started; completed-but-unnotified ones as
                    # task_completed (marked notified after delivery).
                    if flags.is_enabled("bg_continuity"):
                        await self._replay_user_tasks(websocket, user_id)
                    logger.info(
                        "perf register_ui.total duration_ms=%d user=%s",
                        int((time.monotonic() - _register_started) * 1000), user_id)
                else:
                    logger.warning("UI registration failed: Invalid or missing token")
                    # Feature 016 (FR-015): When the client said it was
                    # silently resuming (resumed=True) but the server
                    # rejected the token, record auth.session_resume_failed
                    # so the audit log captures the failure. Best-effort
                    # attribution via base64-decode of the JWT payload; on
                    # failure record as anonymous.
                    try:
                        resumed_flag = bool(getattr(msg, "resumed", False))
                        if resumed_flag:
                            attribution_claims = None
                            if token:
                                try:
                                    import base64
                                    parts = token.split(".")
                                    if len(parts) == 3:
                                        payload_b64 = parts[1]
                                        payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
                                        payload_json = base64.b64decode(payload_b64).decode("utf-8")
                                        attribution_claims = json.loads(payload_json)
                                except Exception:
                                    attribution_claims = None
                            from audit.hooks import record_auth_event
                            from audit.recorder import get_recorder, make_correlation_id, now_utc
                            from audit.schemas import AuditEventCreate
                            if attribution_claims and attribution_claims.get("sub"):
                                await record_auth_event(
                                    claims=attribution_claims,
                                    action="session_resume_failed",
                                    description="Silent session resume rejected (invalid/expired token)",
                                    outcome="failure",
                                    outcome_detail="ws_register token rejected",
                                )
                            else:
                                rec = get_recorder()
                                if rec is not None:
                                    await rec.record(AuditEventCreate(
                                        actor_user_id="anonymous",
                                        auth_principal="anonymous",
                                        event_class="auth",
                                        action_type="auth.session_resume_failed",
                                        description="Silent session resume rejected; token unattributable",
                                        correlation_id=make_correlation_id(),
                                        outcome="failure",
                                        outcome_detail="ws_register token rejected (no claims recoverable)",
                                        inputs_meta={"resumed": True},
                                        started_at=now_utc(),
                                    ))
                    except Exception as _e:
                        logger.debug(f"session_resume_failed audit record failed: {_e}")
                    # Ungate waiting tasks so they hit the auth check naturally
                    evt = self._registered_events.get(id(websocket))
                    if evt:
                        evt.set()
                    # Feature 028 (FR-009, research D4): replace the dead-end
                    # error Alert with a recoverable auth_required signal. The
                    # client re-fetches /auth/session (which silently
                    # refreshes server-side) and retries register_ui, or
                    # redirects to /auth/login when the session is truly gone.
                    from shared.protocol import AuthRequired
                    reason = "invalid"
                    if token:
                        try:
                            import base64 as _b64
                            _p = token.split(".")[1]
                            _p += "=" * (-len(_p) % 4)
                            _exp = json.loads(_b64.urlsafe_b64decode(_p)).get("exp")
                            if _exp is not None and float(_exp) < time.time():
                                reason = "expired"
                        except Exception:
                            pass
                    await self._safe_send(websocket, AuthRequired(reason=reason).to_json())
                    return

            elif isinstance(msg, VoicePlayoutEvent):
                await self._handle_voice_playout_event(websocket, msg)

            elif msg.type in ("llm_config_set", "llm_config_clear"):
                # Feature 006-user-llm-config: per-user LLM credential
                # set/clear over WS. Both require an authenticated socket.
                if websocket not in self.ui_sessions:
                    await self._safe_send(websocket, json.dumps({
                        "type": "error",
                        "code": "unauthenticated",
                        "message": "register_ui must complete before llm_config_*",
                    }))
                    return
                claims = self.ui_sessions.get(websocket) or {}
                actor_user_id = claims.get("sub") or "legacy"
                auth_principal = (
                    claims.get("preferred_username")
                    or claims.get("sub")
                    or "unknown"
                )
                from llm_config.ws_handlers import (
                    handle_llm_config_set,
                    handle_llm_config_clear,
                )
                if msg.type == "llm_config_set":
                    saved = await handle_llm_config_set(
                        safe_send=self._safe_send,
                        websocket=websocket,
                        config=getattr(msg, "config", {}) or {},
                        actor_user_id=actor_user_id,
                        auth_principal=auth_principal,
                        store=self._llm_store,
                        recorder=self.audit_recorder,
                    )
                    if saved:
                        # Feature 054: a successful save unblocks every one
                        # of the user's gated sockets (FR-015).
                        try:
                            from orchestrator import llm_gate
                            await llm_gate.unlock_after_save(self, actor_user_id)
                        except Exception:
                            logger.warning("llm gate unlock failed", exc_info=True)
                else:
                    removed = await handle_llm_config_clear(
                        safe_send=self._safe_send,
                        websocket=websocket,
                        actor_user_id=actor_user_id,
                        auth_principal=auth_principal,
                        store=self._llm_store,
                        recorder=self.audit_recorder,
                    )
                    if removed:
                        # Feature 054: clearing re-gates immediately — there
                        # is no default to revert to (FR-009/FR-013).
                        try:
                            from orchestrator import llm_gate
                            await llm_gate.regate_after_clear(self, actor_user_id)
                        except Exception:
                            logger.warning("llm gate re-gate failed", exc_info=True)

            elif isinstance(msg, UIEvent):
                # The _registered_events gate guarantees register_ui has already
                # resolved before any ui_event runs. So an unauthenticated socket
                # here means register_ui FAILED — and that path already sent the
                # recoverable `auth_required` frame that drives the client's
                # silent re-auth (028 FR-009 D4). A concurrently-gated ui_event
                # (e.g. the client's initial get_history) arriving in that
                # cold-boot window must NOT paint a dead-end "Unauthorized" alert:
                # it is stale the instant re-auth succeeds and the query works.
                # Drop it silently; the client re-sends its ui_events after the
                # auth_required-driven reconnect.
                if websocket not in self.ui_sessions:
                    logger.debug(
                        "Dropping pre-auth ui_event %r; register_ui already "
                        "signaled auth_required for recovery",
                        getattr(msg, "action", None))
                    return

                user_id = self._get_user_id(websocket)

                # Audit: record the WS UI action in the user's audit log
                try:
                    from audit.hooks import record_ws_action
                    _audit_payload = msg.payload or {}
                    _action_chat_id = msg.session_id or _audit_payload.get(
                        "chat_id"
                    )
                    _voice_audit_origin = _audit_payload.get("voice_origin")
                    if (
                        msg.action == "chat_message"
                        and isinstance(_voice_audit_origin, dict)
                    ):
                        # The final transcript already follows the ordinary
                        # message-retention policy.  Do not create a second
                        # PHI-bearing copy (or retain its digest/proof) in WS
                        # audit metadata; only immutable correlation fences
                        # are operationally necessary here (065 FR-046/047).
                        _audit_payload = {
                            "voice_origin": {
                                key: _voice_audit_origin.get(key)
                                for key in (
                                    "schema_version",
                                    "session_id",
                                    "generation",
                                    "media_grant_revision",
                                    "turn_id",
                                    "client_turn_id",
                                    "chat_context_revision",
                                )
                            },
                            "chat_id": _action_chat_id,
                            "submission_id": str(msg.submission_id or ""),
                            "request_generation": str(
                                msg.request_generation or ""
                            ),
                        }
                    asyncio.create_task(record_ws_action(
                        claims=self.ui_sessions.get(websocket),
                        action=str(msg.action or ""),
                        chat_id=_action_chat_id,
                        payload=_audit_payload,
                    ))
                except Exception as _e:
                    logger.debug(f"WS action audit record failed: {_e}")

                if msg.action == "chat_message":
                    user_message = msg.payload.get("message", "")
                    chat_id = msg.session_id or msg.payload.get("chat_id")
                    draft_agent_id = msg.payload.get("draft_agent_id")
                    voice_origin = msg.voice_origin
                    if voice_origin is None:
                        await self._retire_welcome_canvas(websocket)
                    # Feature 013 / FR-018, FR-024: in-chat tool picker
                    # selection narrows the orchestrator's tool list. None
                    # / absent ≡ no narrowing (existing default behavior).
                    # An empty list reaching this point is a defensive
                    # case (UI gate FR-021 should have blocked send) —
                    # logged at WARN below in handle_chat_message.
                    selected_tools_raw = msg.payload.get("selected_tools")
                    if selected_tools_raw is None or selected_tools_raw == "":
                        selected_tools = None
                    elif isinstance(selected_tools_raw, list):
                        selected_tools = [str(t) for t in selected_tools_raw]
                    else:
                        selected_tools = None

                    voice_dispatch = None
                    if voice_origin is not None:
                        if not chat_id:
                            await self._reject_voice_submission(
                                websocket,
                                user_id=user_id,
                                origin=voice_origin,
                                submission_id=str(msg.submission_id or ""),
                                request_generation=str(
                                    msg.request_generation or ""
                                ),
                                chat_id=str(chat_id or voice_origin.session_id),
                                connection_generation=str(
                                    msg.connection_generation or ""
                                ),
                                reason="invalid_binding",
                                retry_policy="none",
                            )
                            return
                        voice_dispatch = await self._admit_voice_chat_message(
                            websocket,
                            msg,
                            user_id=user_id,
                            chat_id=chat_id,
                            message=user_message,
                        )
                        if voice_dispatch is None:
                            return
                        user_message = voice_dispatch.admission.canonical_text
                    # If no chat_id provided, create one for ordinary typed
                    # chat only. Voice is permanently bound to its recognized
                    # origin and may never resurrect a deleted destination.
                    elif not chat_id:
                        chat_id = await asyncio.to_thread(
                            self.history.create_chat, user_id=user_id)
                        # Inform UI about new chat ID
                        await self._safe_send(websocket, json.dumps({
                            "type": "chat_created",
                            "payload": {"chat_id": chat_id, "from_message": True}
                        }))
                    else:
                        if not await asyncio.to_thread(
                                self.history.get_chat, chat_id, user_id=user_id):
                            await asyncio.to_thread(
                                self.history.create_chat, chat_id, user_id=user_id)
                            await self._safe_send(websocket, json.dumps({
                                "type": "chat_created",
                                "payload": {"chat_id": chat_id, "from_message": True}
                            }))

                    # Feature 028: chat_message also marks this socket's active
                    # chat (pre-028 only load_chat did) so workspace upserts in
                    # brand-new chats reach the originating tab's siblings too.
                    if voice_origin is None:
                        self._ws_active_chat[id(websocket)] = chat_id

                    display_message = msg.payload.get("display_message")
                    async_mode = (
                        False
                        if voice_origin is not None
                        else msg.payload.get("async_mode", False)
                    )

                    # Feature 031: structured attachment references staged on
                    # this turn. Each entry: {attachment_id, filename, category}.
                    # Validated for ownership inside handle_chat_message; absent
                    # / non-list ≡ no attachments (backward compatible).
                    attachments_raw = msg.payload.get("attachments")
                    attachments = attachments_raw if isinstance(attachments_raw, list) else None

                    # 020-async-queries: if async_mode is True, dispatch as
                    # a background task instead of blocking the WS.
                    if async_mode:
                        await self._dispatch_async_chat(
                            websocket, user_message, chat_id, display_message,
                            user_id=user_id, draft_agent_id=draft_agent_id,
                            selected_tools=selected_tools, attachments=attachments,
                        )
                    else:
                        self.cancelled_sessions[id(websocket)] = False
                        # Use serialized wrapper so concurrent chat messages
                        # for the same session are processed one at a time.
                        await self._serialized_chat(
                            websocket, user_message, chat_id, display_message,
                            user_id=user_id, draft_agent_id=draft_agent_id,
                            selected_tools=selected_tools, attachments=attachments,
                            voice_dispatch=voice_dispatch,
                        )

                elif msg.action == "cancel_task":
                    self.cancelled_sessions[id(websocket)] = True
                    # Feature 014 (FR-020/021): mark every in-flight step as
                    # cancelled so the persistent step trail reflects user
                    # intent immediately. Best-effort — late-arriving tool
                    # results are dropped via recorder.is_terminal() checks.
                    recorder = self._chat_recorders.get(id(websocket))
                    if recorder is not None:
                        try:
                            await recorder.cancel_all_in_flight()
                        except Exception:  # pragma: no cover — defensive
                            logger.debug("cancel_all_in_flight failed", exc_info=True)
                    await self._safe_send(websocket, json.dumps({
                        "type": "chat_status",
                        "status": "done",
                        "message": "Cancelled"
                    }))

                elif msg.action == "watch_task":
                    # 020-async-queries: subscribe to task completion notifications
                    task_id = msg.payload.get("task_id")
                    if task_id:
                        bg_task = await self.async_task_manager.get(task_id)
                        if bg_task:
                            task_status = bg_task._canonical_status()
                            # If already completed, notify immediately
                            if task_status.value in (
                                "completed",
                                "failed",
                                "cancelled",
                                "retryable",
                            ):
                                await self._safe_send(websocket, json.dumps({
                                    "type": "task_completed",
                                    "payload": {
                                        "task_id": bg_task.task_id,
                                        "chat_id": bg_task.chat_id,
                                        "status": task_status.value,
                                    },
                                }))
                            else:
                                bg_task.watchers.append(websocket)
                        else:
                            await self._safe_send(websocket, json.dumps({
                                "type": "error",
                                "payload": {"message": f"Task {task_id} not found"},
                            }))
                    else:
                        await self._safe_send(websocket, json.dumps({
                            "type": "error",
                            "payload": {"message": "task_id is required for watch_task"},
                        }))

                elif msg.action == "component_feedback":
                    # Feature 004 — submit feedback for a rendered component.
                    from feedback.ws_handlers import handle_component_feedback
                    claims = self.ui_sessions.get(websocket) or {}
                    auth_principal = claims.get("preferred_username") or claims.get("sub") or "unknown"
                    chat_id = msg.session_id or msg.payload.get("chat_id")
                    await handle_component_feedback(
                        safe_send=self._safe_send,
                        websocket=websocket,
                        payload=msg.payload or {},
                        actor_user_id=user_id,
                        auth_principal=auth_principal,
                        recorder=self.feedback_recorder,
                        conversation_id=chat_id,
                    )

                elif msg.action == "feedback_retract":
                    from feedback.ws_handlers import handle_feedback_retract
                    claims = self.ui_sessions.get(websocket) or {}
                    auth_principal = claims.get("preferred_username") or claims.get("sub") or "unknown"
                    await handle_feedback_retract(
                        safe_send=self._safe_send,
                        websocket=websocket,
                        payload=msg.payload or {},
                        actor_user_id=user_id,
                        auth_principal=auth_principal,
                        recorder=self.feedback_recorder,
                    )

                elif msg.action == "feedback_amend":
                    from feedback.ws_handlers import handle_feedback_amend
                    claims = self.ui_sessions.get(websocket) or {}
                    auth_principal = claims.get("preferred_username") or claims.get("sub") or "unknown"
                    await handle_feedback_amend(
                        safe_send=self._safe_send,
                        websocket=websocket,
                        payload=msg.payload or {},
                        actor_user_id=user_id,
                        auth_principal=auth_principal,
                        recorder=self.feedback_recorder,
                    )

                elif msg.action == "get_dashboard":
                    await self.send_dashboard(websocket)

                elif msg.action == "discover_agents":
                    await self.send_agent_list(websocket)

                elif msg.action == "register_external_agent":
                    # Register an external A2A agent by URL (entered by user in frontend)
                    agent_url = msg.payload.get("url", "").strip().rstrip("/")
                    if not agent_url:
                        await self.send_ui_render(websocket, [
                            Alert(message="Please provide an agent URL", variant="error").to_dict()
                        ])
                    else:
                        await self._safe_send(websocket, json.dumps({
                            "type": "chat_status",
                            "status": "thinking",
                            "message": f"Discovering agent at {agent_url}..."
                        }))
                        await self.discover_a2a_agent(agent_url)
                        if any(aid for aid, card in self.agent_cards.items()
                               if card.metadata.get("a2a_url") == agent_url):
                            await self.send_agent_list(websocket)
                            await self._safe_send(websocket, json.dumps({
                                "type": "chat_status", "status": "done",
                                "message": f"External agent registered from {agent_url}"
                            }))
                        else:
                            await self.send_ui_render(websocket, [
                                Alert(message=f"Could not discover A2A agent at {agent_url}", variant="error").to_dict()
                            ])
                            await self._safe_send(websocket, json.dumps({
                                "type": "chat_status", "status": "done",
                                "message": "Discovery failed"
                            }))

                elif msg.action == "agent_tunnel":
                    # 058 (BYO agents, Mode 1 transport): a user's desktop-hosted
                    # agent tunnels its frames over the owner's authenticated UI
                    # socket. Unwrap and route to the agent-message router.
                    await self._handle_agent_tunnel(websocket, msg)

                elif msg.action == "get_history":
                    # Feature 037: show the server-driven skeleton while the
                    # recent-chats query runs, then push the rendered list.
                    await self._push_history_surface(websocket, loading=True)
                    chats = await asyncio.to_thread(
                        self.history.get_recent_chats, user_id=user_id)
                    await self._safe_send(websocket, json.dumps({
                        "type": "history_list",
                        "chats": chats
                    }))
                    await self._push_history_surface(websocket, chats=chats)

                elif msg.action == "load_chat":
                    chat_id = msg.payload.get("chat_id")
                    chat = await asyncio.to_thread(
                        self.history.get_chat, chat_id, user_id=user_id)
                    if chat:
                        # 001-tool-stream-ui (US2 T042): pause any push
                        # streams this websocket has in its previous chat
                        # before sending chat_loaded. The streams transition
                        # to DORMANT and become eligible for US3 resume on
                        # return.
                        ws_id = id(websocket)
                        old_chat_id = self._ws_active_chat.get(ws_id)
                        if old_chat_id and old_chat_id != chat_id and self.stream_manager is not None:
                            try:
                                await self.stream_manager.pause_chat(websocket, old_chat_id)
                            except Exception as e:
                                logger.warning(f"pause_chat failed: {e}")
                        self._ws_active_chat[ws_id] = chat_id
                        # Loading a chat replaces the canvas — the welcome
                        # blank-on-first-message must not fire afterwards.
                        self._ws_welcome.pop(ws_id, None)
                        # Feature 028 (FR-031/FR-032): switching chats ends any
                        # historical timeline view — the new chat opens live,
                        # and the client is told so its banner/mode clears.
                        if self._ws_timeline_mode.pop(ws_id, None):
                            await self._safe_send(websocket, json.dumps({
                                "type": "workspace_timeline_mode",
                                "active": False,
                            }))

                        # Feature 060: the atomic snapshot is the authoritative
                        # load completion. The bounded chat_loaded/ui_render
                        # pair below remains compatibility-only and is scoped
                        # as a disposable overlay for 060 clients.
                        load_authority = self._conversation_authority(
                            _CONNECTION_OPERATION_CONTEXT.get(), websocket
                        )
                        if load_authority is not None:
                            load_operation, _load_owner, _load_fence = load_authority
                            try:
                                await self._emit_hydration_snapshot(
                                    websocket,
                                    chat_id=chat_id,
                                    user_id=user_id,
                                    connection_generation=(
                                        load_operation.connection_generation
                                    ),
                                    request_generation=(
                                        load_operation.request_generation
                                    ),
                                )
                            except ConversationNotFound:
                                await self._safe_send(
                                    websocket,
                                    json.dumps(
                                        {
                                            "type": "error",
                                            "code": "chat_not_found",
                                            "message": "Chat not found",
                                            "chat_id": chat_id,
                                            "connection_generation": str(
                                                load_operation.connection_generation
                                            ),
                                            "request_generation": str(
                                                load_operation.request_generation
                                            ),
                                        }
                                    ),
                                )
                                return
                            except Exception:
                                logger.warning(
                                    "load_chat atomic hydration failed",
                                    exc_info=True,
                                )
                                await self._safe_send(
                                    websocket,
                                    json.dumps(
                                        {
                                            "type": "error",
                                            "code": "snapshot_retryable",
                                            "message": "Conversation restore is temporarily unavailable",
                                            "chat_id": chat_id,
                                            "connection_generation": str(
                                                load_operation.connection_generation
                                            ),
                                            "request_generation": str(
                                                load_operation.request_generation
                                            ),
                                            "retryable": True,
                                        }
                                    ),
                                )
                                return

                        # Feature 028 (FR-028) + 045: component-bearing transcript
                        # messages get a server-rendered html form, but the chat
                        # rail is TEXT ONLY — only text primitives render; rich
                        # components (tables/charts/metrics) are dropped here and
                        # shown on the canvas, which re-hydrates from the
                        # workspace below. A message with no text-only content
                        # gets no html (the client renders no bubble for it).
                        def _hydrate_loaded_chat():
                            """Render transcript HTML and re-attach chips off the event loop."""
                            try:
                                for m in chat.get("messages", []):
                                    if not isinstance(m.get("content"), str) and isinstance(m.get("content"), list):
                                        _t_html = self._transcript_html(m["content"])
                                        if _t_html:
                                            m["html"] = _t_html
                            except Exception:
                                logger.exception("webrender unavailable for transcript rendering")

                            # Feature 031: re-hydrate per-turn attachment references
                            # so the client re-renders attachment chips on loaded
                            # user messages (additive `attachments` field).
                            try:
                                from orchestrator.attachments.message_attachment_repo import MessageAttachmentRepository
                                from orchestrator.attachments.repository import AttachmentRepository
                                _link_repo = MessageAttachmentRepository(self.history.db)
                                _att_repo = AttachmentRepository(self.history.db)
                                for m in chat.get("messages", []):
                                    if m.get("role") != "user" or not m.get("id"):
                                        continue
                                    links = _link_repo.list_for_message(m["id"], user_id)
                                    atts = []
                                    for ln in links:
                                        a = _att_repo.get_by_id(ln["attachment_id"], user_id)
                                        if a is not None:
                                            atts.append({"attachment_id": a.attachment_id,
                                                         "filename": a.filename, "category": a.category})
                                    if atts:
                                        m["attachments"] = atts
                            except Exception:
                                logger.debug("attachment re-hydration failed (non-fatal)", exc_info=True)

                        await asyncio.to_thread(_hydrate_loaded_chat)

                        await self._safe_send(websocket, json.dumps({
                            "type": "chat_loaded",
                            "chat": chat
                        }))

                        # Feature 028 (FR-027): re-hydrate the persistent
                        # workspace — the canvas state the user left — as a
                        # full ui_render after chat_loaded (stream-resume
                        # precedent below). No capabilities re-run.
                        try:
                            # Feature 029: materialized arrangements re-hydrate too.
                            ws_components = await asyncio.to_thread(
                                self._canvas_components, chat_id, user_id)
                            if ws_components:
                                # speak=False: re-hydration re-presents old
                                # turns — never re-spoken (FR-030).
                                await self.send_ui_render(websocket, ws_components, speak=False)
                        except Exception:
                            logger.exception("workspace re-hydration failed for chat %s", chat_id)

                        # Stream re-attachment (dormant resume + active-stream
                        # attach) — shared with the register_ui session resume
                        # (055 bg-continuity).
                        await self._resume_chat_streams(websocket, user_id, chat_id)

                        # 055 bg-continuity: a chat with a live background run
                        # shows the running state to the joining device.
                        if flags.is_enabled("bg_continuity"):
                            await self._replay_chat_task(websocket, chat_id)
                    else:
                        await self.send_ui_render(websocket, [
                            Alert(message="Chat not found", variant="error").to_dict()
                        ])

                elif msg.action == "new_chat":
                    chat_id = self.history.create_chat(user_id=user_id)
                    if isinstance(msg, CorrelatedNewChat):
                        await self._safe_send(
                            websocket,
                            ChatCreated(
                                connection_generation=(
                                    msg.connection_generation or ""
                                ),
                                submission_id=msg.submission_id or "",
                                request_generation=(
                                    msg.request_generation or ""
                                ),
                                payload={
                                    "schema_version": msg.schema_version,
                                    "chat_id": chat_id,
                                    "from_message": False,
                                    "connection_generation": (
                                        msg.connection_generation
                                    ),
                                    "submission_id": msg.submission_id,
                                    "request_generation": (
                                        msg.request_generation
                                    ),
                                },
                            ).to_json(),
                        )
                    else:
                        await self._safe_send(websocket, json.dumps({
                            "type": "chat_created",
                            "payload": {
                                "chat_id": chat_id,
                                "from_message": False,
                            },
                        }))

                # Feature 054 (FR-014): LLM-dependent workspace/component
                # verbs are refused server-side while the acting user has no
                # LLM configuration, regardless of client behavior. (The
                # combine/condense execution itself runs on the SYSTEM
                # credential; this gate is about the unconfigured USER.)
                elif msg.action in ("combine_components", "condense_components",
                                    "component_action") \
                        and user_id \
                        and not await self.llm_configured_for(user_id):
                    actor_user_id, auth_principal = self._llm_audit_principals(websocket)
                    await self._record_llm_unconfigured(
                        self.audit_recorder,
                        actor_user_id=actor_user_id,
                        auth_principal=auth_principal,
                        feature=f"ui_event:{msg.action}",
                    )
                    await self.send_ui_render(websocket, [
                        Alert(message="Set up your AI provider to use this.",
                              variant="error").to_dict()
                    ], target="chat")

                # Saved components actions
                elif msg.action in ("save_component", "delete_saved_component",
                                    "combine_components", "condense_components") \
                        and self._ws_timeline_mode.get(id(websocket)):
                    # Feature 028 (FR-031): historical views are strictly
                    # read-only — the shipped client makes these unreachable in
                    # timeline mode, but a raw WS client must be refused too.
                    await self._audit_workspace_denial(
                        user_id, msg.payload.get("chat_id") or "",
                        msg.payload.get("component_id") or "", "timeline_readonly")
                    await self.send_ui_render(websocket, [
                        Alert(message="You are viewing a past workspace state — return to live to interact.",
                              variant="warning").to_dict()
                    ], target="chat")

                elif msg.action == "save_component":
                    chat_id = msg.payload.get("chat_id")
                    component_data = msg.payload.get("component_data")
                    component_type = msg.payload.get("component_type")
                    title = msg.payload.get("title")
                    
                    if not chat_id or not component_data:
                        await self.send_ui_render(websocket, [
                            Alert(message="Missing required fields for saving component", variant="error").to_dict()
                        ])
                        return
                    
                    try:
                        # Explicit save is a deprecated alias for a normal
                        # workspace upsert.  A revisioned canvas accepts only
                        # semantic component objects; legacy bare rows cannot
                        # bypass the conversation publication boundary.
                        if not isinstance(component_data, dict):
                            raise ValueError("component_data must be a component object")
                        ops = await self.workspace.aupsert(
                            chat_id, user_id, [component_data]
                        )
                        if not ops:
                            raise RuntimeError("component save produced no workspace mutation")
                        component_id = ops[0]["component_id"]
                        await self.workspace.asnapshot(
                            chat_id, user_id, cause="save"
                        )
                        await self.send_ui_upsert(
                            websocket, chat_id, user_id, ops
                        )

                        # Send success response
                        await self._safe_send(websocket, json.dumps({
                            "type": "component_saved",
                            "component": {
                                "id": component_id,
                                "chat_id": chat_id,
                                "component_data": component_data,
                                "component_type": component_type,
                                "title": title or component_type.replace('_', ' ').title(),
                                "created_at": int(time.time() * 1000)
                            }
                        }))
                        
                        # Broadcast updated chat history (each user gets their own)
                        await self._broadcast_user_history()

                    except Exception as e:
                        logger.error(f"Failed to save component: {e}")
                        await self._safe_send(websocket, json.dumps({
                            "type": "component_save_error",
                            "error": str(e)
                        }))

                elif msg.action == "get_saved_components":
                    chat_id = msg.payload.get("chat_id")
                    components = self.history.get_saved_components(chat_id, user_id=user_id)
                    await self._safe_send(websocket, json.dumps({
                        "type": "saved_components_list",
                        "components": components
                    }))

                elif msg.action == "delete_saved_component":
                    component_id = msg.payload.get("component_id")
                    if not component_id:
                        await self.send_ui_render(websocket, [
                            Alert(message="Missing component ID", variant="error").to_dict()
                        ])
                        return

                    # Resolve the legacy physical row to its stable workspace
                    # identity, then remove only from the complete staged
                    # canvas.  The outer operation publishes that removal.
                    row = await asyncio.to_thread(
                        self.history.get_component_by_id,
                        component_id,
                        user_id=user_id,
                    )
                    ws_component_id = None
                    chat_id_for_row = row.get("chat_id") if row else None
                    if not chat_id_for_row:
                        chat_id_for_row = self._ws_active_chat.get(id(websocket))
                    if row and chat_id_for_row:
                        ws_component_id = (
                            await self._workspace_identity_for_saved_row(
                                chat_id=chat_id_for_row,
                                user_id=user_id,
                                row=row,
                                supplied_id=component_id,
                            )
                        )
                    success = bool(
                        ws_component_id
                        and chat_id_for_row
                        and await self.workspace.aremove(
                            chat_id_for_row, user_id, ws_component_id
                        )
                    )
                    if success:
                        await self.workspace.asnapshot(
                            chat_id_for_row, user_id, cause="remove"
                        )
                        await self._safe_send(websocket, json.dumps({
                            "type": "component_deleted",
                            "component_id": component_id
                        }))
                        if ws_component_id and chat_id_for_row:
                            await self.send_ui_upsert(websocket, chat_id_for_row, user_id, [
                                {"op": "remove", "component_id": ws_component_id}
                            ])
                            try:
                                from audit.hooks import record_workspace_event
                                asyncio.create_task(record_workspace_event(
                                    user_id=user_id, action="component_removed",
                                    chat_id=chat_id_for_row, component_id=ws_component_id,
                                ))
                            except Exception:
                                logger.debug("workspace remove audit failed", exc_info=True)

                        # Broadcast updated chat history (each user gets their own)
                        await self._broadcast_user_history()
                    else:
                        await self._safe_send(websocket, json.dumps({
                            "type": "component_save_error",
                            "error": "Component not found"
                        }))

                elif msg.action == "combine_components":
                    source_id = msg.payload.get("source_id")
                    target_id = msg.payload.get("target_id")
                    
                    if not source_id or not target_id:
                        await self._safe_send(websocket, json.dumps({
                            "type": "combine_error",
                            "error": "Both source and target component IDs are required"
                        }))
                        return
                    
                    source = self.history.get_component_by_id(source_id, user_id=user_id)
                    target = self.history.get_component_by_id(target_id, user_id=user_id)
                    
                    if not source or not target:
                        await self._safe_send(websocket, json.dumps({
                            "type": "combine_error",
                            "error": "One or both components not found"
                        }))
                        return
                    
                    # Send progress
                    await self._safe_send(websocket, json.dumps({
                        "type": "combine_status",
                        "status": "combining",
                        "message": f"Combining {source['title']} with {target['title']}..."
                    }))
                    
                    try:
                        result = await self._combine_components_llm(
                            [source, target],
                            mode="combine"
                        )
                        
                        if result.get("error"):
                            await websocket.send(json.dumps({
                                "type": "combine_error",
                                "error": result["error"]
                            }))
                        else:
                            chat_id = source["chat_id"]
                            removed_ids, new_components, ops = (
                                await self._replace_workspace_components(
                                    chat_id=chat_id,
                                    user_id=user_id,
                                    source_rows=[source, target],
                                    replacements=result["components"],
                                    cause="combine",
                                )
                            )
                            await self.send_ui_upsert(
                                websocket, chat_id, user_id, ops
                            )

                            await self._safe_send(websocket, json.dumps({
                                "type": "components_combined",
                                "removed_ids": removed_ids,
                                "new_components": new_components
                            }))
                    except Exception as e:
                        logger.error(f"Combine failed: {e}", exc_info=True)
                        await self._safe_send(websocket, json.dumps({
                            "type": "combine_error",
                            "error": f"Failed to combine components: {str(e)}"
                        }))

                elif msg.action == "get_agent_permissions":
                    agent_id = msg.payload.get("agent_id")
                    if not agent_id:
                        return
                    # Build available tools list for this agent
                    card = self.agent_cards.get(agent_id)
                    if not card:
                        return
                    available_tools = [s.id for s in card.skills]
                    tool_descriptions = {s.id: s.description for s in card.skills}
                    scopes = self.tool_permissions.get_agent_scopes(user_id, agent_id)
                    tool_scope_map = self.tool_permissions.get_tool_scope_map(agent_id)
                    permissions = self.tool_permissions.get_effective_permissions(
                        user_id, agent_id, available_tools
                    )
                    tool_overrides = self.tool_permissions.get_tool_overrides(user_id, agent_id)
                    await self._safe_send(websocket, json.dumps({
                        "type": "agent_permissions",
                        "agent_id": agent_id,
                        "agent_name": card.name,
                        "scopes": scopes,
                        "tool_scope_map": tool_scope_map,
                        "permissions": permissions,
                        "tool_overrides": tool_overrides,
                        "tool_descriptions": tool_descriptions,
                        "security_flags": self.security_flags.get(agent_id, {})
                    }))

                elif msg.action == "set_agent_permissions":
                    agent_id = msg.payload.get("agent_id")
                    scopes = msg.payload.get("scopes", {})
                    tool_overrides_payload = msg.payload.get("tool_overrides")
                    if not agent_id or not isinstance(scopes, dict):
                        return
                    self.tool_permissions.set_agent_scopes(
                        user_id, agent_id, scopes
                    )
                    if isinstance(tool_overrides_payload, dict):
                        self.tool_permissions.set_tool_overrides(
                            user_id, agent_id, tool_overrides_payload
                        )
                    logger.info(f"Scopes updated: user={user_id} agent={agent_id} scopes={scopes}")
                    # Compute effective per-tool permissions from new scopes + overrides
                    card = self.agent_cards.get(agent_id)
                    available_tools = [s.id for s in card.skills] if card else []
                    permissions = self.tool_permissions.get_effective_permissions(
                        user_id, agent_id, available_tools
                    )
                    tool_overrides = self.tool_permissions.get_tool_overrides(user_id, agent_id)
                    await self._safe_send(websocket, json.dumps({
                        "type": "agent_permissions_updated",
                        "agent_id": agent_id,
                        "scopes": scopes,
                        "permissions": permissions,
                        "tool_overrides": tool_overrides
                    }))

                    # Also broadcast an updated dashboard to all UI clients for this user
                    # so their total tools count updates immediately. Feature 008:
                    # also re-broadcast agent_list so the per-user
                    # `tools_available_for_user` flag stays in sync with the new
                    # permissions — this drives the persistent text-only banner
                    # (FR-005, FR-007a).
                    for client in self.ui_clients:
                        client_user_id = self._get_user_id(client)
                        if client_user_id == user_id:
                            asyncio.create_task(self.send_dashboard(client))
                            asyncio.create_task(self.send_agent_list(client))

                elif msg.action == "enable_recommended_agents":
                    # Feature 030 — one-click consent enable. The click IS the
                    # explicit user grant (Constitution VII: the system sets
                    # attenuated scopes automatically; the user may override
                    # per agent afterwards). Server-side validation: only
                    # connected, non-draft, PUBLIC agents are eligible and
                    # ``tools:write`` is never granted. The action is audited
                    # like every other ui_event (ws.enable_recommended_agents).
                    requested = msg.payload.get("agent_ids")
                    if requested is not None and not (
                        isinstance(requested, list)
                        and all(isinstance(a, str) for a in requested)
                    ):
                        return
                    enabled_now = await asyncio.to_thread(
                        self._enable_recommended_agent_scopes, user_id, requested)
                    if self._ws_welcome.get(id(websocket)):
                        # Welcome canvas is showing — re-render it so the
                        # consent card disappears and the examples are live.
                        from orchestrator.welcome import welcome_components
                        await self.send_ui_render(websocket, welcome_components(
                            tools_available=await asyncio.to_thread(
                                self.compute_tools_available_for_user, user_id)))
                    else:
                        await self.send_ui_render(websocket, [Alert(
                            message=(
                                f"Enabled {len(enabled_now)} agents for this account "
                                "(read-only — never write) — ask your question again to use them."
                                if enabled_now else
                                "No public agents were available to enable."
                            ),
                            variant="success" if enabled_now else "warning",
                        ).to_dict()], target="chat")
                    for client in self.ui_clients:
                        if self._get_user_id(client) == user_id:
                            asyncio.create_task(self.send_dashboard(client))
                            asyncio.create_task(self.send_agent_list(client))

                elif msg.action == "schedule_decision":
                    # Feature 030 — the consent click for a chat-proposed
                    # scheduled job (audited as ws.schedule_decision plus the
                    # schedule.* events the handler records).
                    from orchestrator import scheduling_chat
                    await scheduling_chat.handle_decision(
                        self, websocket, user_id, msg.payload or {})

                elif msg.action == "remote_op_decision":
                    # Feature 063 US3 — the approve/decline click for a proposed
                    # DESTRUCTIVE remote operation (durable proposal; single-use).
                    from orchestrator import remote_confirmation
                    await remote_confirmation.handle_decision(
                        self, websocket, user_id, msg.payload or {})

                elif msg.action == "update_device":
                    # ROTE: viewport / capability change from the frontend
                    device_info = msg.payload.get("device") or {}
                    # Capture the pre-change profile so we can diff the canvas
                    # adaptation and push only what actually changed.
                    old_profile = self.rote.get_profile(websocket)
                    new_profile, re_adapted, profile_changed = self.rote.update_device(websocket, device_info)
                    await self._safe_send(websocket, json.dumps({
                        "type": "rote_config",
                        "device_profile": new_profile.to_dict(),
                        "speech_server_available": bool(os.getenv("SPEACHES_URL", "").strip()),
                    }))
                    # A device change re-renders the FULL persisted workspace
                    # from server state. A single-slot _last_components replay
                    # would wipe all but the most recent fragment once partial
                    # upserts exist.
                    handled_via_workspace = False
                    if profile_changed:
                        active_chat = self._ws_active_chat.get(id(websocket))
                        if active_chat:
                            try:
                                # Re-adapt the designed canvas, not just the
                                # flat component list.
                                ws_components = self._canvas_components(active_chat, user_id)
                                if ws_components:
                                    # When the live-viewport flag is on, push
                                    # only the components whose adaptation
                                    # actually changed (targeted upsert to THIS
                                    # socket — other devices didn't change). Any
                                    # failure falls through to the full re-render.
                                    if viewport.viewport_enabled() and await self._readapt_targeted(
                                            websocket, active_chat, old_profile, new_profile, ws_components):
                                        handled_via_workspace = True
                                    else:
                                        # speak=False: viewport re-adaptation of
                                        # existing content — never re-spoken.
                                        await self.send_ui_render(websocket, ws_components,
                                                                  speak=False)
                                        handled_via_workspace = True
                            except Exception:
                                logger.exception("workspace re-adapt failed after device change")
                    # Legacy fallback for sockets with no persisted workspace.
                    if not handled_via_workspace and re_adapted is not None:
                        re_html = None
                        try:
                            from webrender import render_for_target, target_for_profile
                            _re_prof = self.rote.get_profile(websocket)
                            re_html = render_for_target(target_for_profile(_re_prof), re_adapted, _re_prof)
                        except Exception:
                            logger.exception("webrender: failed to re-render UI after device change")
                        msg_out = UIUpdate(components=re_adapted, html=re_html)
                        await self._safe_send(websocket, msg_out.to_json())

                elif msg.action == "save_theme":
                    # Persist theme colors to user preferences
                    theme_data = msg.payload.get("theme")
                    if theme_data:
                        try:
                            await asyncio.to_thread(
                                self.history.db.set_user_preferences,
                                user_id, {"theme": theme_data})
                        except Exception as e:
                            logger.warning(f"Failed to save theme for {user_id}: {e}")

                elif msg.action == "condense_components":
                    component_ids = msg.payload.get("component_ids", [])
                    logger.info(f"Condense requested: {len(component_ids)} component IDs for user={user_id}")

                    try:
                        if len(component_ids) < 2:
                            await self._safe_send(websocket, json.dumps({
                                "type": "combine_error",
                                "error": "At least 2 components are required to condense"
                            }))
                            return

                        components = []
                        for cid in component_ids:
                            comp = await asyncio.to_thread(
                                self.history.get_component_by_id, cid, user_id=user_id)
                            if comp:
                                components.append(comp)

                        if len(components) < 2:
                            logger.warning(f"Condense: only {len(components)} of {len(component_ids)} components found in DB")
                            await self._safe_send(websocket, json.dumps({
                                "type": "combine_error",
                                "error": "Not enough valid components found"
                            }))
                            return

                        await self._safe_send(websocket, json.dumps({
                            "type": "combine_status",
                            "status": "condensing",
                            "message": f"Condensing {len(components)} components..."
                        }))

                        result = await self._combine_components_llm(
                            components,
                            mode="condense"
                        )

                        if result.get("error"):
                            await self._safe_send(websocket, json.dumps({
                                "type": "combine_error",
                                "error": result["error"]
                            }))
                        else:
                            chat_id = components[0]["chat_id"]
                            removed_ids, new_components, ops = (
                                await self._replace_workspace_components(
                                    chat_id=chat_id,
                                    user_id=user_id,
                                    source_rows=components,
                                    replacements=result["components"],
                                    cause="condense",
                                )
                            )
                            await self.send_ui_upsert(
                                websocket, chat_id, user_id, ops
                            )

                            await self._safe_send(websocket, json.dumps({
                                "type": "components_condensed",
                                "removed_ids": removed_ids,
                                "new_components": new_components
                            }))
                    except Exception as e:
                        logger.error(f"Condense failed: {e}", exc_info=True)
                        await self._safe_send(websocket, json.dumps({
                            "type": "combine_error",
                            "error": f"Failed to condense components: {str(e)}"
                        }))

                elif msg.action == "component_action":
                    # Feature 028 — standardized deterministic component
                    # action (contracts/component-action.md).
                    await self._handle_component_action(websocket, user_id, msg.payload or {})

                elif msg.action == "component_refine":
                    # 055 US4 (wire-contract §3) — component-scoped LLM edit
                    # in place; gated + refused inside the handler.
                    await self._handle_component_refine(websocket, user_id, msg.payload or {})

                elif msg.action == "component_restore":
                    # 055 US4 — restore an archived component_version under
                    # the same identity (no LLM).
                    await self._handle_component_restore(websocket, user_id, msg.payload or {})

                elif msg.action == "authorize_action":
                    # C-S8 — the user confirmed a require_token-gated call: mint a
                    # one-time token and re-dispatch the call through the normal
                    # tool gate (which verifies + consumes it).
                    _p = msg.payload or {}
                    _aid, _tool = _p.get("agent_id"), _p.get("tool")
                    _targs = dict(_p.get("args") or {})
                    _tok = self.mint_action_token(_aid, user_id, _tool, _targs)
                    if _tok and _tool:
                        from types import SimpleNamespace as _SNS
                        _targs["_txn_token"] = _tok
                        _tc = _SNS(id="authz", function=_SNS(
                            name=_tool, arguments=json.dumps(_targs)))
                        await self.execute_single_tool(
                            websocket, _tc, {_tool: _aid}, _p.get("chat_id"), user_id=user_id)
                    else:
                        await self.send_ui_render(websocket, [Alert(
                            message="Couldn't authorize that action.", variant="warning").to_dict()])

                elif msg.action == "table_paginate":
                    # Feature 028 (FR-038): pagination clicks that carry the
                    # table's component identity route through the
                    # standardized pipeline — permission-gated and updating
                    # ONLY the table, instead of replacing the whole canvas.
                    if (msg.payload or {}).get("component_id"):
                        await self._handle_component_action(websocket, user_id, {
                            "chat_id": (msg.payload or {}).get("chat_id"),
                            "component_id": msg.payload["component_id"],
                            "kind": "refresh",
                            "params_patch": (msg.payload or {}).get("params", {}),
                        })
                        return
                    # Legacy alias (pre-028 clients): re-invoke with raw params.
                    tool_name = msg.payload.get("tool_name")
                    agent_id = msg.payload.get("agent_id")
                    params = msg.payload.get("params", {})

                    if not tool_name or not agent_id:
                        await self.send_ui_render(websocket, [
                            Alert(message="Missing tool_name or agent_id for pagination", variant="error").to_dict()
                        ])
                        await self._safe_send(websocket, json.dumps({
                            "type": "chat_status", "status": "done", "message": ""
                        }))
                        return

                    # Inject per-user credentials (E2E encrypted — only agent can decrypt)
                    args = dict(params)
                    if user_id and agent_id:
                        creds = self.credential_manager.get_agent_credentials_encrypted(user_id, agent_id)
                        if creds:
                            args["_credentials"] = creds
                            args["_credentials_encrypted"] = True

                    try:
                        result = await self._execute_with_retry(websocket, agent_id, tool_name, args)
                        if result and result.ui_components:
                            await self.send_ui_render(websocket, result.ui_components)
                        elif result and result.error:
                            await self.send_ui_render(websocket, [
                                Alert(message=result.error.get("message", "Pagination failed"), variant="error").to_dict()
                            ])
                    except Exception as e:
                        logger.error(f"table_paginate failed: {e}", exc_info=True)
                        await self.send_ui_render(websocket, [
                            Alert(message=f"Pagination failed: {e}", variant="error").to_dict()
                        ])
                    finally:
                        await self._safe_send(websocket, json.dumps({
                            "type": "chat_status", "status": "done", "message": ""
                        }))

                # --- Live Streaming ---
                elif msg.action == "stream_subscribe":
                    # 001-tool-stream-ui: route based on the tool's declared
                    # kind. PUSH tools go through StreamManager (the new
                    # async-generator path); POLL tools stay on the existing
                    # _handle_stream_subscribe path. The kind comes from the
                    # tool's metadata, populated at register_agent time.
                    tool_name = msg.payload.get("tool_name", "")
                    tool_cfg = self._streamable_tools.get(tool_name, {})
                    kind = tool_cfg.get("kind", "poll")

                    if kind == "push":
                        if not flags.is_enabled("tool_streaming"):
                            await self._safe_send(websocket, json.dumps({
                                "type": "stream_error",
                                "request_action": "stream_subscribe",
                                "session_id": msg.session_id,
                                "payload": {
                                    "tool_name": tool_name,
                                    "code": "not_streamable",
                                    "message": "Push streaming is not enabled (FF_TOOL_STREAMING)",
                                },
                            }))
                        else:
                            await self._handle_push_stream_subscribe(
                                websocket, msg.session_id, msg.payload, user_id
                            )
                    elif flags.is_enabled("live_streaming"):
                        await self._handle_stream_subscribe(websocket, msg.payload)
                    else:
                        await self._safe_send(websocket, json.dumps({
                            "type": "stream_error", "tool_name": tool_name,
                            "error": "Live streaming is not enabled"
                        }))

                elif msg.action == "stream_unsubscribe":
                    # 001-tool-stream-ui: dual routing as above. The push
                    # path takes a stream_id; the poll path takes a tool_name.
                    payload = msg.payload or {}
                    if payload.get("stream_id") and flags.is_enabled("tool_streaming"):
                        await self._handle_push_stream_unsubscribe(
                            websocket, msg.session_id, payload, user_id
                        )
                    elif flags.is_enabled("live_streaming"):
                        await self._handle_stream_unsubscribe(websocket, payload)

                elif msg.action == "stream_list":
                    if flags.is_enabled("live_streaming"):
                        await self._handle_stream_list(websocket)

                else:
                    # Feature 027: chrome/settings + agentic-creation actions
                    # live in their own dispatcher. It returns False only for
                    # actions outside its namespace — those were previously a
                    # silent fall-through; log them so typos are diagnosable.
                    from orchestrator.chrome_events import handle_chrome_event
                    handled = await handle_chrome_event(
                        self, websocket, str(msg.action or ""), msg.payload or {}, user_id
                    )
                    if not handled:
                        logger.warning("Unhandled ui_event action: %r", msg.action)
                        if _CONNECTION_OPERATION_CONTEXT.get() is not None:
                            raise RuntimeError("ui_event action is not supported")

        except Exception as e:
            if (
                isinstance(raw_frame, dict)
                and raw_frame.get("type") == "voice_playout_event"
            ):
                # This telemetry/control evidence carries no user-facing
                # operation. Malformed observations fail closed without a
                # generic error frame, audit entry, or task side effect.
                logger.warning(
                    "voice_playout_event_rejected reason=%s",
                    getattr(e, "code", type(e).__name__),
                )
                return
            # Feature 060 credential Save owns its terminal at the durable
            # operation wrapper.  Preserve that typed, safe outcome across
            # the legacy chrome dispatcher instead of swallowing it as a
            # generic UI error (which would later fabricate completion).
            from llm_config.ws_handlers import LLMConfigOperationFailure
            if isinstance(e, LLMConfigOperationFailure):
                raise
            import traceback
            logger.error(f"Error handling UI message: {e}\n{traceback.format_exc()}")
            # Feature 044 (FR-002/SC-006): a generic ui_event failure must not be
            # invisible — every client shows error frames, so the turn reaches a
            # terminal state instead of a permanent "thinking".
            try:
                await self._safe_send(websocket, json.dumps({
                    "type": "error",
                    "code": "internal",
                    "message": "The request failed on the server. Please retry.",
                }))
            except Exception:
                logger.debug("error-frame emission failed", exc_info=True)
            if _CONNECTION_OPERATION_CONTEXT.get() is not None:
                raise

    # =========================================================================
    # COMPONENT COMBINING (LLM-powered)
    # =========================================================================

    async def _workspace_identity_for_saved_row(
        self,
        *,
        chat_id: str,
        user_id: str,
        row: Dict[str, Any],
        supplied_id: str | None = None,
    ) -> str | None:
        """Map a legacy saved-row reference onto the staged stable identity."""

        data = row.get("component_data")
        if isinstance(data, dict):
            identity = data.get("component_id") or row.get("component_id")
            if isinstance(identity, str) and identity:
                return identity
        if supplied_id:
            semantic = await self.workspace.aget_by_component_id(
                chat_id, user_id, supplied_id
            )
            if semantic is not None:
                return supplied_id
        if not isinstance(data, dict):
            return None
        source_semantic = dict(data)
        source_semantic.pop("component_id", None)
        candidates: list[str] = []
        for candidate in await self.workspace.alive_rows(chat_id, user_id):
            candidate_data = candidate.get("component_data")
            if not isinstance(candidate_data, dict):
                continue
            candidate_semantic = dict(candidate_data)
            candidate_semantic.pop("component_id", None)
            if (
                candidate.get("created_at") == row.get("created_at")
                and candidate_semantic == source_semantic
                and isinstance(candidate.get("component_id"), str)
            ):
                candidates.append(candidate["component_id"])
        return candidates[0] if len(candidates) == 1 else None

    async def _replace_workspace_components(
        self,
        *,
        chat_id: str,
        user_id: str,
        source_rows: List[Dict[str, Any]],
        replacements: List[Dict[str, Any]],
        cause: str = "replace",
    ) -> tuple[list[str], list[Dict[str, Any]], list[Dict[str, Any]]]:
        """Replace saved component identities inside the active publication."""

        source_ids: list[str] = []
        source_tools: set[str] = set()
        source_agents: set[str] = set()
        for row in source_rows:
            if str(row.get("chat_id") or "") != chat_id:
                raise ValueError("component replacement crosses conversations")
            data = row.get("component_data")
            if not isinstance(data, dict):
                raise ValueError("component replacement source is not semantic")
            component_id = await Orchestrator._workspace_identity_for_saved_row(
                self,
                chat_id=chat_id,
                user_id=user_id,
                row=row,
            )
            if not isinstance(component_id, str) or not component_id:
                raise ValueError("component replacement source has no stable identity")
            if component_id not in source_ids:
                source_ids.append(component_id)
            if isinstance(data.get("_source_tool"), str) and data["_source_tool"]:
                source_tools.add(data["_source_tool"])
            if isinstance(data.get("_source_agent"), str) and data["_source_agent"]:
                source_agents.add(data["_source_agent"])

        for component_id in source_ids:
            if not await self.workspace.aremove(chat_id, user_id, component_id):
                raise RuntimeError("component replacement lost its source identity")

        semantic: list[Dict[str, Any]] = []
        normalized_specs: list[Dict[str, Any]] = []
        source_tool = sorted(source_tools)[0] if source_tools else None
        source_agent = sorted(source_agents)[0] if source_agents else None
        for spec in replacements:
            if not isinstance(spec, dict) or not isinstance(
                spec.get("component_data"), dict
            ):
                raise ValueError("replacement component is malformed")
            data = dict(spec["component_data"])
            if source_tool:
                data["_source_tool"] = source_tool
            if source_agent:
                data["_source_agent"] = source_agent
            semantic.append(data)
            normalized_specs.append(spec)
        if not semantic:
            raise ValueError("component replacement must produce a component")
        ops = await self.workspace.aupsert(chat_id, user_id, semantic)
        if len(ops) != len(semantic):
            raise RuntimeError("component replacement was incomplete")
        await self.workspace.asnapshot(chat_id, user_id, cause=cause)

        rows: list[Dict[str, Any]] = []
        for op, spec in zip(ops, normalized_specs):
            row = await self.workspace.aget_by_component_id(
                chat_id, user_id, op["component_id"]
            )
            if row is None:
                raise RuntimeError("replacement component is not staged")
            data = row["component_data"]
            component_type = str(
                spec.get("component_type")
                or (data.get("type") if isinstance(data, dict) else "")
                or "combined"
            )
            rows.append(
                {
                    "id": row["id"],
                    "chat_id": chat_id,
                    "component_data": data,
                    "component_type": component_type,
                    "title": str(
                        spec.get("title")
                        or row.get("title")
                        or component_type.replace("_", " ").title()
                    ),
                    "created_at": row.get("created_at") or int(time.time() * 1000),
                }
            )
        return source_ids, rows, ops

    async def _combine_components_llm(self, components: list, mode: str = "combine") -> dict:
        """Use LLM to combine/condense UI components.
        
        Args:
            components: List of component dicts with component_data, title, etc.
            mode: 'combine' for merging 2 components, 'condense' for reducing many.
        
        Returns:
            {"components": [...]} on success, {"error": "..."} on failure.
        """
        # Feature 054: combine/condense is a SYSTEM-context helper by
        # explicit owner decision (websocket=None is passed to _call_llm
        # below, which resolves the admin-managed system credential —
        # never the acting user's record). This fast-path return just
        # avoids building a long prompt that would never be sent; the
        # downstream _call_llm emits llm_unconfigured when the system
        # credential is also absent.
        if await self._llm_store.get_system() is None:
            return {"error": "LLM not configured"}

        # Build the component descriptions for the prompt
        component_descriptions = []
        for i, comp in enumerate(components):
            component_descriptions.append(
                f"Component {i+1} (title: \"{comp['title']}\", type: \"{comp['component_type']}\"):\n"
                f"```json\n{json.dumps(comp['component_data'], indent=2)}\n```"
            )
        
        components_text = "\n\n".join(component_descriptions)

        schema_description = """Available UI primitive types and their JSON structure:
- "text": {type: "text", content: "...", variant: "body|h1|h2|h3|caption|markdown"}
- "card": {type: "card", title: "...", content: [...child components...]}
- "metric": {type: "metric", title: "...", value: "...", subtitle: "...", progress: 0.0-1.0, variant: "default|warning|error|success"}
- "table": {type: "table", title: "...", headers: [...], rows: [[...],...]}
- "grid": {type: "grid", columns: 2, gap: 16, children: [...child components...]}
- "container": {type: "container", children: [...child components...]}
- "list": {type: "list", items: [...], ordered: false, variant: "default|detailed"}
- "alert": {type: "alert", message: "...", title: "...", variant: "info|success|warning|error"}
- "progress": {type: "progress", value: 0.0-1.0, label: "...", show_percentage: true}
- "bar_chart": {type: "bar_chart", title: "...", labels: [...], datasets: [{label: "...", data: [...]}]}
- "line_chart": {type: "line_chart", title: "...", labels: [...], datasets: [{label: "...", data: [...]}]}
- "pie_chart": {type: "pie_chart", title: "...", labels: [...], data: [...], colors: [...]}
- "code": {type: "code", code: "...", language: "..."}
- "divider": {type: "divider"}
- "collapsible": {type: "collapsible", title: "...", content: [...child components...], default_open: false}"""

        if mode == "combine":
            prompt = f"""You are a UI component combiner. You are given 2 UI components and must merge them into a single cohesive component.

{schema_description}

RULES:
1. Analyze whether these components can be meaningfully combined.
2. If they contain RELATED data (e.g., patient data + disease chart, or multiple system metrics), combine them into a unified component using cards, grids, or tables.
3. If they are UNRELATED or incompatible, respond ONLY with: ERROR: <brief reason>
4. Preserve ALL data — do not lose any information from either component.
5. Use grid layouts to arrange related metrics side-by-side.
6. Use cards with descriptive titles to group related content.

COMPONENTS TO COMBINE:

{components_text}

Respond with ONLY valid JSON (no markdown code fences) in this format:
{{
  "components": [
    {{
      "component_data": {{...the merged component tree...}},
      "component_type": "card",
      "title": "Descriptive Title For Merged Component"
    }}
  ]
}}

Or if they cannot be combined:
ERROR: <reason>"""
        else:  # condense
            prompt = f"""You are a UI component condenser. You are given {len(components)} UI components and must combine as many as possible into fewer cohesive components.

{schema_description}

RULES:
1. Group RELATED components together (e.g., all system metrics into one dashboard card, all patient data into one view).
2. Keep UNRELATED components separate — don't force unrelated data together.
3. Preserve ALL data — do not lose any information.
4. Use grid layouts to arrange related metrics side-by-side.
5. Use cards with descriptive titles to group related content.
6. The goal is to REDUCE the total number of components while maintaining clarity.

COMPONENTS TO CONDENSE:

{components_text}

Respond with ONLY valid JSON (no markdown code fences) in this format:
{{
  "components": [
    {{
      "component_data": {{...component tree...}},
      "component_type": "card",
      "title": "Descriptive Title"
    }}
  ]
}}"""

        try:
            # Use _call_llm for built-in retries (important for transient 502s)
            llm_msg, _usage = await self._call_llm(
                None,  # no websocket needed for combine
                [
                    {"role": "system", "content": "You are a precise UI component combiner. Output ONLY valid JSON or an ERROR message. No explanations, no markdown fences."},
                    {"role": "user", "content": prompt}
                ],
                tools_desc=None,
                temperature=0.1
            )

            if not llm_msg:
                return {"error": "LLM returned no response"}
            
            content = (llm_msg.content or "").strip()
            logger.info(f"LLM combine response ({len(content)} chars): {content[:200]}...")
            
            # Check for ERROR response
            if content.upper().startswith("ERROR"):
                error_msg = content.split(":", 1)[1].strip() if ":" in content else content
                return {"error": error_msg}
            
            # Try to parse JSON
            # Strip markdown code fences if present
            if content.startswith("```"):
                content = content.split("\n", 1)[1] if "\n" in content else content[3:]
                if content.endswith("```"):
                    content = content[:-3]
                content = content.strip()
            
            try:
                result = json.loads(content)
            except json.JSONDecodeError:
                # Try to find JSON in the response
                json_match = re.search(r'\{[\s\S]*\}', content)
                if json_match:
                    result = json.loads(json_match.group())
                else:
                    return {"error": "Failed to parse LLM response as JSON"}
            
            if "components" not in result or not isinstance(result["components"], list):
                return {"error": "LLM response missing 'components' array"}
            
            # Feature 029 (FR-020): the renderer registry is the single
            # source of truth for valid types — hand-copied whitelists
            # drifted (param_picker/audio/file IO were silently rewritten
            # to containers). "chart" stays as an accepted alias; the tree
            # validator maps it to plotly_chart.
            from webrender import allowed_primitive_types
            VALID_TYPES = set(allowed_primitive_types()) | {"chart"}
            
            # Validate each component
            for comp in result["components"]:
                if "component_data" not in comp:
                    return {"error": "LLM response component missing 'component_data'"}
                
                # Validate the component type
                comp_data = comp["component_data"]
                comp_type = comp_data.get("type", "")
                if comp_type and comp_type not in VALID_TYPES:
                    logger.warning(f"LLM produced unknown component type '{comp_type}', wrapping in card")
                    # Wrap unknown types in a card to ensure they render
                    comp["component_data"] = {
                        "type": "card",
                        "title": comp_data.get("title", "Combined Component"),
                        "content": [comp_data] if comp_type else []
                    }
                    comp_type = "card"
                
                # Recursively validate children
                self._validate_component_tree(comp_data, VALID_TYPES)
                
                if "component_type" not in comp:
                    comp["component_type"] = comp_type or "card"
                if "title" not in comp:
                    comp["title"] = comp["component_data"].get("title", "Combined Component")
            
            return result
            
        except Exception as e:
            logger.error(f"LLM combine error: {e}", exc_info=True)
            return {"error": f"LLM error: {str(e)}"}

    def _validate_component_tree(self, node: dict, valid_types: set):
        """Recursively validate component tree, fixing invalid types."""
        if not isinstance(node, dict):
            return
        
        raw_type = node.get("type", "")
        node_type = raw_type.strip().lower()
        # Map generic 'chart' to 'plotly_chart' regardless of validity
        if node_type == "chart":
            logger.info("Mapping generic component type 'chart' -> 'plotly_chart'")
            node["type"] = "plotly_chart"
            node_type = "plotly_chart"  # update variable for subsequent checks
        
        if node_type and node_type not in valid_types:
            logger.warning(f"Fixing unknown component type '{node_type}' -> 'container'")
            node["type"] = "container"
        
        # Validate children arrays
        for key in ("children", "content"):
            children = node.get(key, [])
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        self._validate_component_tree(child, valid_types)
        
        # Validate tab items
        tabs = node.get("tabs", [])
        if isinstance(tabs, list):
            for tab in tabs:
                if isinstance(tab, dict):
                    for child in tab.get("content", []):
                        if isinstance(child, dict):
                            self._validate_component_tree(child, valid_types)

    # Component types that carry no rich visual content (just text wrappers)
    _TEXT_ONLY_TYPES = {"text", "card", "container", "collapsible", "divider", "list", "alert"}

    @classmethod
    def _is_text_only_components(cls, components: list) -> bool:
        """Return True if all components in the tree contain only text-based content.

        Used to decide whether parsed UI JSON should go to the canvas (rich content)
        or the chat panel only (text-only content).
        """
        for comp in components:
            if not isinstance(comp, dict):
                continue
            comp_type = comp.get("type", "").strip().lower()
            if comp_type not in cls._TEXT_ONLY_TYPES:
                return False
            for key in ("children", "content"):
                children = comp.get(key, [])
                if isinstance(children, list):
                    child_dicts = [c for c in children if isinstance(c, dict) and "type" in c]
                    if child_dicts and not cls._is_text_only_components(child_dicts):
                        return False
        return True

    @classmethod
    def _transcript_html(cls, content) -> str:
        """Feature 045 — server-rendered HTML for a component-bearing transcript
        message, restricted to TEXT ONLY.

        The chat rail is words only: rich components (tables, charts, metrics,
        dashboards, …) live on the canvas and re-hydrate from the persistent
        workspace (``_canvas_components``), NOT from the transcript. So a loaded
        transcript message renders only its text-only primitives (Text/Alert/
        List and text-only containers); any rich component is dropped. Returns
        ``''`` when the message carries nothing text-like — the client then
        renders no bubble for it (the content is on the canvas).
        """
        if not isinstance(content, list):
            return ""
        text_only = [c for c in content
                     if isinstance(c, dict) and cls._is_text_only_components([c])]
        if not text_only:
            return ""
        try:
            from webrender import render as _render_web
            return _render_web(text_only)
        except Exception:
            logger.debug("transcript text-only render failed", exc_info=True)
            return ""

    def _map_file_paths(self, chat_id: str, args: Dict, user_id: str = 'legacy') -> Dict:
        """Replace original filenames in tool arguments with backend paths.

        Uses file mappings stored in history for the given chat.
        """
        if not chat_id:
            return args

        mappings = self.history.get_file_mappings(chat_id, user_id=user_id)
        if not mappings:
            return args

        # Build mapping dict: original_name -> backend_path
        mapping_dict = {m["original_name"]: m["backend_path"] for m in mappings}

        # Recursively traverse args dict and replace strings that match original names
        def replace_in_dict(obj):
            if isinstance(obj, dict):
                return {k: replace_in_dict(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [replace_in_dict(item) for item in obj]
            elif isinstance(obj, str):
                # Check if the string matches any original name (exact match)
                for orig, backend in mapping_dict.items():
                    if obj == orig:
                        logger.info(f"Mapping file path: '{orig}' -> '{backend}'")
                        return backend
                return obj
            else:
                return obj

        new_args = replace_in_dict(args)
        if new_args != args:
            logger.info(f"Mapped file paths in tool arguments for chat {chat_id}")
        return new_args

    # =========================================================================
    # LLM-POWERED TOOL ROUTING
    # =========================================================================

    @staticmethod
    def _conversation_authority(operation_context, websocket):
        """Return one complete fenced turn authority or ``None`` for legacy work."""

        authority = operation_context
        if authority is None:
            authority = getattr(websocket, "task", None)
        if isinstance(authority, dict):
            operation = authority.get("operation")
            owner = authority.get("owner")
            fence = authority.get("execution_fence")
        else:
            operation = getattr(authority, "_operation", None)
            owner = getattr(authority, "_owner", None)
            fence = getattr(authority, "_execution_fence", None)
        if not (
            isinstance(operation, (OperationRecord, SafeOperationProjection))
            and isinstance(owner, OperationOwner)
            and isinstance(fence, ExecutionFence)
            and operation.request_generation is not None
            and operation.connection_generation is not None
        ):
            return None
        return operation, owner, fence

    def _bind_conversation_scope(
        self,
        websocket,
        *,
        chat_id: str,
        connection_generation: Any,
        request_generation: Any,
        purpose: str,
        base_render_revision: int,
    ) -> Dict[str, Any]:
        """Bind the exact active conversation generation for one socket."""

        if purpose not in {"hydration", "commit"}:
            raise ValueError("conversation scope purpose is invalid")
        connection = _uuid.UUID(str(connection_generation))
        request = _uuid.UUID(str(request_generation))
        chat = _uuid.UUID(str(chat_id))
        if any(value.version != 4 for value in (connection, request, chat)):
            raise ValueError("conversation scope identities must be UUID4")
        if (
            isinstance(base_render_revision, bool)
            or not isinstance(base_render_revision, int)
            or base_render_revision < 0
        ):
            raise ValueError("base_render_revision must be non-negative")
        binding = {
            "chat_id": str(chat),
            "connection_generation": str(connection),
            "request_generation": str(request),
            "purpose": purpose,
            "base_render_revision": base_render_revision,
            "frame_sequence": 0,
        }
        self._conversation_scopes[id(websocket)] = binding
        return binding

    async def _begin_conversation_publication(
        self,
        websocket,
        *,
        chat_id: str,
        user_id: str,
        operation_context: Any,
    ):
        """Prepare an invisible complete next revision for a fenced live turn."""

        authority = self._conversation_authority(operation_context, websocket)
        if authority is None:
            return None, None, None
        operation, owner, fence = authority
        if str(operation.chat_id or "") != str(chat_id):
            raise RuntimeError("conversation operation chat identity changed")
        request_generation = str(operation.request_generation)
        connection_generation = str(operation.connection_generation)
        staged = None
        try:
            layouts = await asyncio.to_thread(
                self.workspace.live_layouts, chat_id, user_id
            )
            staged = await asyncio.to_thread(
                self.conversation_commits.stage_commit,
                chat_id=chat_id,
                owner_user_id=user_id,
                request_generation=request_generation,
                operation_fence=fence,
                operation_owner=owner,
                connection_generation=connection_generation,
            )
            await asyncio.to_thread(
                self.conversation_commits.prepare_canvas_stage,
                commit_id=staged["commit_id"],
                owner_user_id=user_id,
                operation_fence=fence,
            )
            from orchestrator.conversation_publication import (
                ConversationPublicationStage,
                activate_conversation_publication,
            )

            stage = ConversationPublicationStage(
                history=self.history,
                commit_id=staged["commit_id"],
                chat_id=chat_id,
                user_id=user_id,
                base_render_revision=staged["base_render_revision"],
                next_render_revision=staged["base_render_revision"] + 1,
                operation_fence=fence,
                layouts=layouts,
            )
            token = activate_conversation_publication(stage)
            self._bind_conversation_scope(
                websocket,
                chat_id=chat_id,
                connection_generation=connection_generation,
                request_generation=request_generation,
                purpose="commit",
                base_render_revision=stage.base_render_revision,
            )
            self._ws_active_chat[id(websocket)] = chat_id
            return stage, token, request_generation
        except BaseException:
            if staged is not None:
                try:
                    await asyncio.to_thread(
                        self.conversation_commits.abort_commit,
                        commit_id=staged["commit_id"],
                        owner_user_id=user_id,
                    )
                except Exception:
                    logger.debug(
                        "conversation stage cleanup failed",
                        exc_info=True,
                    )
            raise

    async def _begin_voice_conversation_publication(
        self,
        websocket,
        *,
        chat_id: str,
        user_id: str,
        user_content: Any,
        operation_context: Any,
        voice_dispatch: _VoiceDispatchContext,
    ):
        """Commit voice acceptance and activate its linked private result.

        Only the user bubble, the complete accepted canvas/layout view, the
        assistant-result stage, and the content-free voice correlation share
        this short transaction. Model/tool execution starts after the chat row
        lock is released and mutates only the result stage.
        """

        authority = self._conversation_authority(
            operation_context, websocket
        )
        if authority is None:
            raise RuntimeError("voice publication lacks execution authority")
        operation, owner, fence = authority
        turn = voice_dispatch.admission.turn
        if (
            str(operation.chat_id or "") != str(chat_id)
            or str(operation.request_generation or "")
            != str(turn.request_generation)
        ):
            raise RuntimeError("voice publication identity changed")
        result_request_generation = turn.result_request_generation
        if result_request_generation is None:
            raise RuntimeError(
                "voice result request generation is unavailable"
            )
        services = getattr(self, "voice_services", None)
        if services is None:
            raise RuntimeError("voice services became unavailable")

        def _accept_turn(**correlation):
            return services.repository.accept_transcript(
                user_id=user_id,
                turn_id=turn.turn_id,
                message_id=correlation["message_id"],
                accepted_connection_generation=(
                    voice_dispatch.connection_generation
                ),
                acceptance_commit_id=correlation[
                    "acceptance_commit_id"
                ],
                result_commit_id=correlation["result_commit_id"],
                operation_id=str(operation.operation_id),
                now=datetime.now(UTC),
                transaction=correlation["cursor"],
            )

        accepted = await asyncio.to_thread(
            self.conversation_commits.accept_voice_turn,
            chat_id=chat_id,
            owner_user_id=user_id,
            request_generation=str(turn.request_generation),
            result_request_generation=str(result_request_generation),
            connection_generation=voice_dispatch.connection_generation,
            user_content=user_content,
            operation_fence=fence,
            operation_owner=owner,
            accept_turn=_accept_turn,
        )
        accepted_turn = accepted.get("accepted_turn")
        if accepted_turn is None:
            accepted_turn = await asyncio.to_thread(
                services.repository.get_turn,
                user_id=user_id,
                turn_id=turn.turn_id,
            )
        accepted_turn_record = getattr(accepted_turn, "turn", accepted_turn)

        from orchestrator.conversation_publication import (
            ConversationPublicationStage,
            activate_conversation_publication,
        )

        acceptance_record = accepted["acceptance"]
        acceptance_stage = ConversationPublicationStage(
            history=self.history,
            commit_id=acceptance_record["commit_id"],
            chat_id=chat_id,
            user_id=user_id,
            base_render_revision=acceptance_record[
                "base_render_revision"
            ],
            next_render_revision=acceptance_record[
                "committed_render_revision"
            ],
            operation_fence=fence,
            layouts=accepted["layouts"],
            publication_role="user_acceptance",
        )
        acceptance_stage.seal(committed=True)

        result_record = accepted["result"]
        result_stage = ConversationPublicationStage(
            history=self.history,
            commit_id=result_record["commit_id"],
            chat_id=chat_id,
            user_id=user_id,
            base_render_revision=result_record["base_render_revision"],
            next_render_revision=(
                result_record["base_render_revision"] + 1
            ),
            operation_fence=fence,
            layouts=accepted["layouts"],
            publication_role="assistant_result",
            execution_base_render_revision=(
                result_record["execution_base_render_revision"]
            ),
        )
        token = activate_conversation_publication(result_stage)
        return (
            result_stage,
            token,
            str(result_request_generation),
            acceptance_stage,
            acceptance_record,
            accepted_turn_record,
            int(accepted["message_id"]),
        )

    async def _begin_detached_conversation_publication(
        self,
        *,
        chat_id: str,
        user_id: str,
        request_generation: Any,
    ):
        """Prepare a server-originated logical update without a UI operation.

        Detached results have no still-running connection operation to fence,
        but they retain the same atomic conversation boundary and are
        serialized under the per-chat workspace lock by their caller.
        """

        staged = None
        try:
            layouts = await asyncio.to_thread(
                self.workspace.live_layouts, chat_id, user_id
            )
            staged = await asyncio.to_thread(
                self.conversation_commits.stage_commit,
                chat_id=chat_id,
                owner_user_id=user_id,
                request_generation=request_generation,
            )
            if staged["state"] != "staged":
                raise RuntimeError("detached conversation publication is terminal")
            await asyncio.to_thread(
                self.conversation_commits.prepare_canvas_stage,
                commit_id=staged["commit_id"],
                owner_user_id=user_id,
            )
            from orchestrator.conversation_publication import (
                ConversationPublicationStage,
                activate_conversation_publication,
            )

            stage = ConversationPublicationStage(
                history=self.history,
                commit_id=staged["commit_id"],
                chat_id=chat_id,
                user_id=user_id,
                base_render_revision=staged["base_render_revision"],
                next_render_revision=staged["base_render_revision"] + 1,
                layouts=layouts,
            )
            return stage, activate_conversation_publication(stage)
        except BaseException:
            if staged is not None and staged.get("state") == "staged":
                try:
                    await asyncio.to_thread(
                        self.conversation_commits.abort_commit,
                        commit_id=staged["commit_id"],
                        owner_user_id=user_id,
                    )
                except Exception:
                    logger.debug(
                        "detached conversation stage cleanup failed",
                        exc_info=True,
                    )
            raise

    async def _begin_scheduled_conversation_publication(
        self,
        *,
        chat_id: str,
        user_id: str,
        scheduled_attempt: "ScheduledAttempt",
    ):
        """Prepare the invisible canvas half of one scheduled chat effect."""

        if (
            scheduled_attempt.execution_fence is None
            or scheduled_attempt.request_generation is None
        ):
            raise RuntimeError("scheduled conversation publication is unfenced")
        owner = OperationOwner(
            owner_scope=OwnerScope.SCHEDULE,
            owner_user_id=user_id,
            connection_scope_id=None,
        )
        staged = None
        try:
            layouts = await asyncio.to_thread(
                self.workspace.live_layouts, chat_id, user_id
            )
            staged = await asyncio.to_thread(
                self.conversation_commits.stage_commit,
                chat_id=chat_id,
                owner_user_id=user_id,
                request_generation=scheduled_attempt.request_generation,
                operation_fence=scheduled_attempt.execution_fence,
                operation_owner=owner,
            )
            await asyncio.to_thread(
                self.conversation_commits.prepare_canvas_stage,
                commit_id=staged["commit_id"],
                owner_user_id=user_id,
                operation_fence=scheduled_attempt.execution_fence,
            )
            from orchestrator.conversation_publication import (
                ConversationPublicationStage,
                activate_conversation_publication,
            )

            stage = ConversationPublicationStage(
                history=self.history,
                commit_id=staged["commit_id"],
                chat_id=chat_id,
                user_id=user_id,
                base_render_revision=staged["base_render_revision"],
                next_render_revision=staged["base_render_revision"] + 1,
                operation_fence=scheduled_attempt.execution_fence,
                layouts=layouts,
            )
            return stage, activate_conversation_publication(stage)
        except BaseException:
            if staged is not None and staged.get("state") == "staged":
                try:
                    await asyncio.to_thread(
                        self.conversation_commits.abort_commit,
                        commit_id=staged["commit_id"],
                        owner_user_id=user_id,
                    )
                except Exception:
                    logger.debug(
                        "scheduled conversation stage cleanup failed",
                        exc_info=True,
                    )
            raise

    async def _append_conversation_message(
        self,
        stage,
        *,
        chat_id: str,
        user_id: str,
        role: str,
        content: Any,
    ) -> Any:
        """Append to the invisible revision, or retain the bounded legacy path."""

        if stage is None:
            await asyncio.to_thread(
                self.history.add_message,
                chat_id,
                role,
                content,
                user_id=user_id,
            )
            from orchestrator.scheduled_publication import (
                current_scheduled_history_stage,
            )

            if current_scheduled_history_stage() is not None:
                return None
            return await asyncio.to_thread(
                self.history.get_latest_message_id,
                chat_id,
                user_id,
            )
        message_id = await asyncio.to_thread(
            self.conversation_commits.append_staged_message,
            commit_id=stage.commit_id,
            owner_user_id=user_id,
            role=role,
            content=content,
            operation_fence=stage.operation_fence,
        )
        stage.mark_dirty()
        return message_id

    def _adapt_conversation_snapshot(self, websocket, snapshot: Dict[str, Any]):
        """ROTE-adapt every component group, then add web presentation once."""

        for message in snapshot["transcript"]:
            for part in message["parts"]:
                if part.get("type") == "components":
                    part["components"] = self.rote.adapt(
                        websocket, part["components"]
                    )
        snapshot["canvas"]["components"] = self.rote.adapt(
            websocket, snapshot["canvas"]["components"]
        )
        profile = self.rote.get_profile(websocket)
        target = "native" if _is_native_device(profile) else "web"
        return augment_conversation_snapshot_for_target(
            snapshot, profile, target=target
        )

    async def _conversation_snapshot_candidate(
        self,
        websocket,
        *,
        chat_id: str,
        user_id: str,
        connection_generation: Any,
        request_generation: Any,
        purpose: str,
    ) -> Dict[str, Any]:
        semantic = await asyncio.to_thread(
            self.conversation_commits.build_snapshot,
            chat_id=chat_id,
            owner_user_id=user_id,
            connection_generation=connection_generation,
            request_generation=request_generation,
            snapshot_purpose=purpose,
        )
        return self._adapt_conversation_snapshot(websocket, semantic)

    async def _emit_hydration_snapshot(
        self,
        websocket,
        *,
        chat_id: str,
        user_id: str,
        connection_generation: Any,
        request_generation: Any,
    ) -> Dict[str, Any]:
        snapshot = await self._conversation_snapshot_candidate(
            websocket,
            chat_id=chat_id,
            user_id=user_id,
            connection_generation=connection_generation,
            request_generation=request_generation,
            purpose="hydration",
        )
        self._bind_conversation_scope(
            websocket,
            chat_id=chat_id,
            connection_generation=connection_generation,
            request_generation=request_generation,
            purpose="hydration",
            base_render_revision=snapshot["render_revision"],
        )
        self._ws_active_chat[id(websocket)] = chat_id
        await self._safe_send(
            websocket,
            json.dumps(
                snapshot,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ),
        )
        return snapshot

    async def _publish_conversation_snapshot(
        self,
        websocket,
        *,
        stage,
        request_generation: str,
        server_initiated: bool = False,
    ) -> Dict[str, Any]:
        raw_components = await asyncio.to_thread(
            self.workspace.live_components, stage.chat_id, stage.user_id
        )
        layouts = await asyncio.to_thread(
            self.workspace.live_layouts, stage.chat_id, stage.user_id
        )
        if stage.publication_role == "assistant_result":
            committed = await asyncio.to_thread(
                self.conversation_commits.publish_voice_result,
                commit_id=stage.commit_id,
                owner_user_id=stage.user_id,
                canvas_components=raw_components,
                canvas_layouts=layouts,
                operation_fence=stage.operation_fence,
            )
        else:
            committed = await asyncio.to_thread(
                self.conversation_commits.publish_commit,
                commit_id=stage.commit_id,
                owner_user_id=stage.user_id,
                messages=None,
                canvas_components=raw_components,
                canvas_layouts=layouts,
                operation_fence=stage.operation_fence,
            )
        if stage.summary_text is not None:
            committed = {
                **committed,
                "summary_text": stage.summary_text,
                "summary_source": stage.summary_source,
            }
        stage.seal(committed=True)

        if stage.snapshot_cause:
            try:
                await self.workspace.asnapshot(
                    stage.chat_id,
                    stage.user_id,
                    cause=stage.snapshot_cause,
                )
            except Exception:
                logger.debug(
                    "post-commit workspace timeline snapshot failed",
                    exc_info=True,
                )

        await self._deliver_committed_conversation_snapshot(
            websocket,
            stage=stage,
            request_generation=request_generation,
            committed=committed,
            server_initiated=server_initiated,
        )
        return committed

    async def _deliver_committed_conversation_snapshot(
        self,
        websocket,
        *,
        stage,
        request_generation: str,
        committed: Dict[str, Any],
        server_initiated: bool,
    ) -> None:
        """Deliver one already-durable revision without mutating its commit."""

        targets = []
        for candidate in self._sockets_on_chat(stage.user_id, stage.chat_id):
            binding = self._conversation_scopes.get(id(candidate))
            if binding is None or binding.get("chat_id") != stage.chat_id:
                continue
            if server_initiated or binding.get("request_generation") == request_generation:
                targets.append((candidate, binding))
        if (
            not server_initiated
            and websocket in self.ui_clients
            and all(candidate is not websocket for candidate, _binding in targets)
        ):
            binding = self._conversation_scopes.get(id(websocket))
            if binding is not None:
                targets.append((websocket, binding))

        for candidate, binding in targets:
            try:
                if server_initiated:
                    connection_generation = binding["connection_generation"]
                    ready = ConversationCommitReady(
                        chat_id=stage.chat_id,
                        connection_generation=connection_generation,
                        request_generation=request_generation,
                        render_revision=committed["committed_render_revision"],
                    )
                    if not await self._safe_send(candidate, ready.to_json()):
                        continue
                    binding = self._bind_conversation_scope(
                        candidate,
                        chat_id=stage.chat_id,
                        connection_generation=connection_generation,
                        request_generation=request_generation,
                        purpose="commit",
                        base_render_revision=stage.base_render_revision,
                    )
                snapshot = await self._conversation_snapshot_candidate(
                    candidate,
                    chat_id=stage.chat_id,
                    user_id=stage.user_id,
                    connection_generation=binding["connection_generation"],
                    request_generation=request_generation,
                    purpose="commit",
                )
                delivered = await self._safe_send(
                    candidate,
                    json.dumps(
                        snapshot,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ),
                )
                if delivered:
                    binding["base_render_revision"] = committed[
                        "committed_render_revision"
                    ]
                    binding["frame_sequence"] = 0
            except Exception:
                # The durable commit remains authoritative; this socket keeps
                # its prior committed view and recovers through hydration.
                logger.warning(
                    "committed conversation snapshot delivery failed",
                    exc_info=True,
                )

        # Recent conversations are a projection of durable commits, not of
        # optional title generation.  In particular, voice acceptance commits
        # the user's first message before model execution begins; if title
        # generation or the assistant result later fails, the accepted chat
        # must still replace the history surface's empty state.  Keep this
        # owner-scoped and fail-soft so presentation fan-out cannot roll back
        # or delay the already-authoritative conversation commit.
        await self._refresh_history_after_commit(stage.user_id)

    def _scope_conversation_transient(self, websocket, data: str) -> str:
        """Attach the current equality fence to disposable live UI frames."""

        binding = getattr(self, "_conversation_scopes", {}).get(id(websocket))
        if binding is None:
            return data
        try:
            frame = json.loads(data)
        except (TypeError, json.JSONDecodeError):
            return data
        if not isinstance(frame, dict) or frame.get("type") not in {
            "ui_render",
            "ui_update",
            "ui_upsert",
            "ui_append",
            "ui_stream_data",
        }:
            return data
        if frame.get("target") == "history":
            return data
        explicit_chat = frame.get("chat_id")
        if explicit_chat not in (None, "", binding["chat_id"]):
            return data
        if getattr(self, "_ws_active_chat", {}).get(id(websocket)) not in (
            None,
            binding["chat_id"],
        ):
            return data
        binding["frame_sequence"] += 1
        frame.update(
            {
                "chat_id": binding["chat_id"],
                "connection_generation": binding["connection_generation"],
                "request_generation": binding["request_generation"],
                "base_render_revision": binding["base_render_revision"],
                "frame_sequence": binding["frame_sequence"],
            }
        )
        return json.dumps(
            frame,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    async def _start_heartbeat(self, websocket) -> asyncio.Task:
        """Start sending heartbeat messages every 5s to keep UI informed during long operations."""
        async def _heartbeat_loop():
            while True:
                await asyncio.sleep(5)
                try:
                    await self._safe_send(websocket, json.dumps({
                        "type": "heartbeat",
                        "timestamp": time.time()
                    }))
                except Exception:
                    break
        return asyncio.create_task(_heartbeat_loop())

    async def _serialized_chat(
        self,
        websocket,
        message,
        chat_id,
        display_message,
        *,
        user_id=None,
        draft_agent_id=None,
        selected_tools=None,
        attachments=None,
        operation_context=None,
        voice_dispatch=None,
    ):
        """Run handle_chat_message under a per-websocket lock so messages
        are serialized but the WS receive loop is never blocked."""
        ws_id = id(websocket)
        try:
            if operation_context is None:
                operation_context = _CONNECTION_OPERATION_CONTEXT.get()
            if voice_dispatch is not None:
                await self.handle_chat_message(
                    websocket, message, chat_id, display_message,
                    user_id=user_id, draft_agent_id=draft_agent_id,
                    selected_tools=selected_tools, attachments=attachments,
                    operation_context=operation_context,
                    voice_dispatch=voice_dispatch,
                )
            else:
                lock = self._chat_locks.setdefault(ws_id, asyncio.Lock())
                async with lock:
                    workspace_locks = getattr(self, "_workspace_locks", None)
                    if workspace_locks is None:
                        workspace_locks = {}
                        self._workspace_locks = workspace_locks
                    workspace_lock = workspace_locks.setdefault(
                        chat_id, asyncio.Lock()
                    )
                    async with workspace_lock:
                        await self.handle_chat_message(
                            websocket, message, chat_id, display_message,
                            user_id=user_id, draft_agent_id=draft_agent_id,
                            selected_tools=selected_tools,
                            attachments=attachments,
                            operation_context=operation_context,
                            voice_dispatch=voice_dispatch,
                        )
        except Exception as e:
            # Full details (including any upstream HTML payload, stack
            # trace, etc.) go to structured logs only. The user-facing
            # message is a generic, safe string — never str(e), which
            # may contain raw HTML, secrets, or PHI from upstream.
            logger.error(f"Chat task error: {e}", exc_info=True)
            # Feature 014: a mid-turn exception left some steps in-flight
            # with no chance to complete. Mark them cancelled so the UI
            # does not show a stuck spinner. (The success path does NOT
            # cancel — every step lifecycle call has already fired by
            # the time handle_chat_message returns; auto-cancelling on
            # the success path produced false-cancel labels on
            # successfully-completed steps.)
            recorder = self._chat_recorders.get(ws_id)
            if recorder is not None:
                try:
                    await recorder.cancel_all_in_flight()
                except Exception:  # pragma: no cover — defensive
                    logger.debug("ChatStepRecorder exception flush failed", exc_info=True)
            await self._safe_send(websocket, json.dumps({
                "type": "chat_status", "status": "done",
                "message": "Something went wrong while processing your request. Please try again."
            }))
            # Surface a clean Alert in the chat so the user sees a
            # tangible response in the message area, matching the
            # FR-008 / FR-009 (006) "LLM unavailable" pattern.
            try:
                await self.send_ui_render(websocket, [
                    Alert(
                        message="Something went wrong while processing your request. Please try again.",
                        variant="error",
                    ).to_dict()
                ])
            except Exception:  # pragma: no cover — defensive
                pass
        finally:
            # Feature 014: clear the per-turn step recorder reference.
            # We do NOT flush in-flight steps here — the success path
            # has already terminated them, and the exception path above
            # explicitly flushes. The cancel_task handler (line ~959)
            # also flushes for genuine user-initiated cancellations.
            # If a programmer error left a step in_progress, the
            # GET /api/chats/{id}/steps endpoint heals stale rows
            # (>30 s old, no active task) into 'interrupted' on the
            # next chat load.
            self._chat_recorders.pop(ws_id, None)

    async def _dispatch_async_chat(
        self, websocket, message: str, chat_id: str, display_message: str = None,
        *, user_id=None, draft_agent_id=None, selected_tools=None, attachments=None,
    ):
        """020-async-queries: Dispatch a chat message as a background task.

        Creates a BackgroundTask and returns immediately with a task_started
        message. The task runs handle_chat_message asynchronously using a
        VirtualWebSocket to capture outputs.
        """
        logger.info("Dispatching async chat for chat_id=%s user_id=%s", chat_id, user_id)

        async def _run_in_background(vws, msg, cid, display, uid, draft, tools, atts):
            """Execute handle_chat_message with the virtual WS."""
            workspace_lock = self._workspace_locks.setdefault(
                cid, asyncio.Lock()
            )
            async with workspace_lock:
                await self.handle_chat_message(
                    vws, msg, cid, display,
                    user_id=uid, draft_agent_id=draft, selected_tools=tools,
                    attachments=atts,
                )

        uid = user_id or self._get_user_id(websocket)
        connection_generation = None
        request_generation = None
        authority = self._conversation_authority(
            _CONNECTION_OPERATION_CONTEXT.get(), websocket
        )
        if authority is not None:
            operation, _owner, _fence = authority
            connection_generation = operation.connection_generation
            request_generation = operation.request_generation
            chat_row = await self.history.db.afetch_one(
                "SELECT render_revision FROM chats WHERE id = ? AND user_id = ?",
                (chat_id, uid),
            )
            if chat_row is None:
                raise ConversationNotFound("conversation not found")
            self._bind_conversation_scope(
                websocket,
                chat_id=chat_id,
                connection_generation=connection_generation,
                request_generation=request_generation,
                purpose="commit",
                base_render_revision=int(chat_row.get("render_revision") or 0),
            )
        # Short user-facing label for cross-device task frames (055).
        title = " ".join((display_message or message or "").split())[:60]
        bg_task = await self.async_task_manager.submit(
            chat_id=chat_id,
            user_id=uid,
            coro_factory=_run_in_background,
            kind="async_chat",
            title=title,
            connection_generation=connection_generation,
            request_generation=request_generation,
            msg=message,
            cid=chat_id,
            display=display_message,
            uid=uid,
            draft=draft_agent_id,
            tools=selected_tools,
            atts=attachments,
        )

        # Register the submitting websocket as a watcher
        bg_task.watchers.append(websocket)

        started = {
            "type": "task_started",
            "payload": {
                "task_id": bg_task.task_id,
                "chat_id": chat_id,
                "status": bg_task._canonical_status().value,
            },
        }
        if flags.is_enabled("bg_continuity"):
            # 055: the start signal reaches every device of the user, not just
            # the originating socket (title labels it on non-chat surfaces).
            started["payload"]["title"] = title
            await self._send_to_user_sockets(uid, started)
        else:
            await self._safe_send(websocket, json.dumps(started))

        # Send loading state so UI knows the query was accepted
        await self._safe_send(websocket, json.dumps({
            "type": "chat_status",
            "status": "processing_async",
            "message": f"Query running in background (task {bg_task.task_id})",
        }))

        return None

    async def derive_machine_authority(self, *, user_id: str,
                                       agent_id: Optional[str],
                                       turn_class: str,
                                       consented_scopes: Optional[List[str]] = None,
                                       grant_id: Optional[str] = None):
        """Derive a machine turn's root authority at the ONE shared seam
        (056 US2, FR-012). Every machine-turn class — scheduled runs, parser
        replay, draft self-tests, and any future class — calls this and nothing
        else, so they cannot drift apart. Returns a ``MachineAuthority`` or an
        ``AuthoritySkip``; callers bind the former with
        :meth:`_bind_machine_turn` and honor the latter fail-closed."""
        from orchestrator.chain_authority import MachineTurnAuthority
        from orchestrator.offline_grant import OfflineGrantStore
        grants = getattr(self, "offline_grants", None) or OfflineGrantStore(self.history.db)
        return await MachineTurnAuthority(self, grants).derive(
            user_id=user_id, agent_id=agent_id,
            consented_scopes=consented_scopes, grant_id=grant_id,
            turn_class=turn_class)

    def _bind_machine_turn(self, vws, authority) -> None:
        """Bind a machine turn's virtual socket to its consent-derived root
        authority (056 US2, FR-012/FR-014/FR-015 — the ONE shared seam).

        Two bindings, both deliberate:

        * ``ui_sessions[vws]`` carries the machine claims plus the fresh
          consent-derived subject token as ``_raw_token``, so the existing RFC
          8693 exchange (``_get_delegation_token``) mints a properly scoped
          delegated token for every real-agent dispatch in the turn — the
          machine turn now acts DELEGATED in production instead of being
          refused fail-closed for having no session token. Any hop the turn
          starts mints children off that same root (one authority model, two
          roots). 055's fan-out already skips VirtualWebSocket entries, so this
          never counts as a connected device.
        * ``vws.machine_claims`` is the per-turn audit marker the dispatch path
          reads, attributing every record to ``machine:<class>`` acting for the
          owning human (never "legacy"). Cost attribution is untouched: a
          VirtualWebSocket turn still resolves the SYSTEM LLM credential (054),
          so who PAID and who AUTHORIZED stay distinct.
        """
        claims = authority.machine_claims()
        try:
            vws.machine_claims = claims
        except Exception:  # pragma: no cover — VirtualWebSocket accepts attrs
            logger.debug("machine claims binding failed", exc_info=True)
        self.ui_sessions[vws] = {**claims, "_raw_token": authority.access_token}

    def _unbind_machine_turn(self, vws) -> None:
        """Drop a machine turn's session binding when the turn ends."""
        self.ui_sessions.pop(vws, None)

    async def run_scheduled_turn(
        self,
        *,
        user_id: str,
        chat_id: Optional[str],
        instruction: str,
        agent_id: Optional[str],
        access_token: str,
        allowed_scopes: List[str],
        correlation_id: str,
        authority=None,
        scheduled_attempt: Optional["ScheduledAttempt"] = None,
        scheduled_store: Optional["ScheduledJobStore"] = None,
        effect_kind: Optional[str] = None,
        effect_key: Optional[str] = None,
        payload_digest: Optional[str] = None,
    ) -> str:
        """Execute a scheduled job's instruction as a background chat turn (025 T040/T046).

        Reached only when ``FF_SCHEDULER_EXECUTION`` is enabled (gated at loop
        start), which itself requires the recorded offline-grant security review
        (030 FR-004/FR-005). The instruction runs through the normal chat path
        with output persisted to ``chat_id`` history, so the user sees it on
        reconnect (in-app only). Returns a short summary for the completion
        notification.

        Authority (056 US2 — the T057-scoped threading, now built): the runner
        derives a :class:`~orchestrator.chain_authority.MachineAuthority` from
        the job's durable consent (fresh token per run, scopes narrowed to
        consented ∩ current) and passes it as ``authority``. It is bound to the
        turn's virtual socket by :meth:`_bind_machine_turn`, so every
        real-agent dispatch in the turn runs under a delegated token derived
        from that consent and every audit row names ``machine:scheduled_job``
        acting for the owning human. Without an ``authority`` the turn still
        runs (unchanged legacy behavior), but production posture refuses its
        real-agent dispatches fail-closed, exactly as before.
        """
        from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket
        from orchestrator.scheduled_publication import stage_scheduled_history

        atomic_inputs = (
            scheduled_attempt,
            scheduled_store,
            effect_kind,
            effect_key,
            payload_digest,
        )
        atomic_publication = all(value is not None for value in atomic_inputs)
        if atomic_publication != any(value is not None for value in atomic_inputs):
            raise ValueError(
                "scheduled publication requires attempt, store, and full effect identity"
            )
        if atomic_publication:
            if str(scheduled_attempt.claim.occurrence_id) != str(correlation_id):
                raise ValueError("scheduled occurrence correlation identity changed")
            if str(scheduled_attempt.job["user_id"]) != str(user_id):
                raise ValueError("scheduled occurrence owner identity changed")
            if {"tools:write", "tools:execute"}.intersection(
                set(allowed_scopes or [])
                | set(scheduled_attempt.job.get("consented_scopes") or [])
            ):
                raise PermissionError(
                    "scheduled mutating tools require reviewed downstream idempotency"
                )
            publication_reservation = await asyncio.to_thread(
                scheduled_store.reserve_atomic_chat_effect,
                scheduled_attempt,
                effect_key=effect_key,
                payload_digest=payload_digest,
            )
            if publication_reservation.state == "published":
                return "Your scheduled task finished."
            if publication_reservation.ambiguous:
                raise RuntimeError("scheduled chat effect outcome is ambiguous")

        # Feature 054 (FR-020): scheduled turns run on the admin-managed
        # system credential. Fail HONESTLY before executing when it is
        # absent — the pre-054 path swallowed the unavailability alert
        # inside the VirtualWebSocket and recorded a "success" run while
        # nothing actually happened.
        if await self._llm_store.get_system() is None:
            try:
                await self._record_llm_unconfigured(
                    self.audit_recorder,
                    actor_user_id=user_id or "system",
                    auth_principal="system",
                    feature="scheduled_job",
                )
            except Exception:  # pragma: no cover — audit is best-effort
                logger.debug("scheduled_job llm_unconfigured audit failed",
                             exc_info=True)
            raise self._LLMUnavailable(
                "no system LLM credential configured — scheduled turn not run")

        target_chat = chat_id or (
            str(scheduled_attempt.job["id"])
            if scheduled_attempt is not None
            else f"scheduled-{user_id}"
        )
        if atomic_publication:
            expected_chat = str(
                scheduled_attempt.job.get("target_chat_id")
                or scheduled_attempt.job["id"]
            )
            if target_chat != expected_chat or effect_key != expected_chat:
                raise ValueError("scheduled target/effect identity changed")
            if effect_kind != "chat_history":
                raise ValueError("scheduled chat requires chat_history effect")
        if (
            not atomic_publication
            and not chat_id
            and flags.is_enabled("bg_continuity")
        ):
            # 055: nothing else creates the fallback chat, and
            # history.add_message silently drops writes to a missing chat —
            # the whole run's output would be lost. Fallback only: an explicit
            # target_chat_id must already exist (chat ids are globally keyed,
            # so creating one here could collide with another user's chat).
            if not await asyncio.to_thread(
                    self.history.get_chat, target_chat, user_id=user_id):
                await asyncio.to_thread(
                    self.history.create_chat, target_chat, user_id=user_id)
        bg = BackgroundTask(
            task_id=(correlation_id or "sched")[:8],
            chat_id=target_chat,
            user_id=user_id,
        )
        vws = VirtualWebSocket(bg)
        if authority is not None:
            self._bind_machine_turn(vws, authority)
        try:
            if atomic_publication:
                if not await asyncio.to_thread(
                    self.history.get_chat, target_chat, user_id=user_id
                ):
                    if chat_id:
                        raise ConversationNotFound("conversation not found")
                    await asyncio.to_thread(
                        self.history.create_chat,
                        target_chat,
                        user_id=user_id,
                        agent_id=agent_id,
                    )
                locks = getattr(self, "_workspace_locks", None)
                if locks is None:
                    locks = {}
                    self._workspace_locks = locks
                stage = None
                token = None
                async with locks.setdefault(target_chat, asyncio.Lock()):
                    try:
                        stage, token = (
                            await self._begin_scheduled_conversation_publication(
                                chat_id=target_chat,
                                user_id=user_id,
                                scheduled_attempt=scheduled_attempt,
                            )
                        )
                        with stage_scheduled_history(
                            history=self.history,
                            chat_id=target_chat,
                            user_id=user_id,
                            create_chat_if_missing=False,
                            agent_id=agent_id,
                        ) as history_stage:
                            await self.handle_chat_message(
                                vws,
                                instruction,
                                target_chat,
                                user_id=user_id,
                            )
                        publication = await asyncio.to_thread(
                            scheduled_store.publish_staged_chat_effect,
                            scheduled_attempt,
                            history_stage.batch(
                                conversation_commit_id=stage.commit_id,
                                request_generation=str(
                                    scheduled_attempt.request_generation
                                ),
                                base_render_revision=stage.base_render_revision,
                                committed_render_revision=stage.next_render_revision,
                                canvas_layouts=stage.layouts,
                            ),
                            effect_kind=effect_kind,
                            effect_key=effect_key,
                            payload_digest=payload_digest,
                        )
                        if publication.state != "published":
                            raise RuntimeError(
                                "scheduled chat publication did not commit"
                            )
                        stage.seal(committed=True)
                        await self._deliver_committed_conversation_snapshot(
                            None,
                            stage=stage,
                            request_generation=str(
                                scheduled_attempt.request_generation
                            ),
                            committed={
                                "committed_render_revision": (
                                    stage.next_render_revision
                                )
                            },
                            server_initiated=True,
                        )
                    finally:
                        if stage is not None and not stage.sealed:
                            try:
                                await asyncio.to_thread(
                                    self.conversation_commits.abort_commit,
                                    commit_id=stage.commit_id,
                                    owner_user_id=stage.user_id,
                                )
                                stage.seal(committed=False)
                            except Exception:
                                logger.warning(
                                    "scheduled conversation stage abort failed",
                                    exc_info=True,
                                )
                        if token is not None:
                            from orchestrator.conversation_publication import (
                                reset_conversation_publication,
                            )

                            reset_conversation_publication(token)
            else:
                await self.handle_chat_message(
                    vws, instruction, target_chat, user_id=user_id
                )
        finally:
            self._unbind_machine_turn(vws)
            try:
                await vws.close()
            except Exception:  # pragma: no cover - close is best-effort
                pass

        # Summarize the captured assistant text for the notification body.
        summary = ""
        for out in bg.outputs:
            if not isinstance(out, dict):
                continue
            txt = out.get("text") or out.get("message")
            if not txt and isinstance(out.get("payload"), dict):
                txt = out["payload"].get("text") or out["payload"].get("message")
            if isinstance(txt, str) and txt.strip():
                summary = txt.strip()
        logger.info(
            "scheduler.run_completed",
            extra={
                "correlation_id": correlation_id,
                "user_id": user_id,
                "chat_id": target_chat,
                "agent_id": agent_id,
                "allowed_scopes": list(allowed_scopes or []),
                "outputs": len(bg.outputs),
            },
        )
        return summary or "Your scheduled task finished."

    async def notify_user(self, user_id: str, payload: Dict[str, Any]) -> None:
        """Deliver an in-app notification to all of a user's connected sockets (025 T049).

        Best-effort live fan-out over ``ui_clients``. The durable artifact of a
        scheduled run is its output, which ``run_scheduled_turn`` persists to chat
        history (delivered on reconnect via ``load_chat``); this is the transient
        toast. In-app only — there is no external channel.
        """
        try:
            data = json.dumps(payload)
        except Exception:  # pragma: no cover - defensive
            logger.debug("notify_user: unserializable payload", exc_info=True)
            return
        sent = 0
        for ws in list(self.ui_clients):
            try:
                if self._get_user_id(ws) != user_id:
                    continue
                if await self._safe_send(ws, data):
                    sent += 1
            except Exception:  # pragma: no cover - per-socket best-effort
                logger.debug("notify_user: send failed for one socket", exc_info=True)
        logger.info(
            "notify_user.delivered",
            extra={"user_id": user_id, "sockets": sent, "kind": payload.get("type")},
        )

    async def _send_to_user_sockets(self, user_id: str, frame: Dict[str, Any]) -> int:
        """Deliver one frame to EVERY connected socket authenticated as
        ``user_id`` (055 bg-continuity — multi-device fan, chat-agnostic).
        Returns the number of sockets reached."""
        try:
            data = json.dumps(frame)
        except Exception:  # pragma: no cover - defensive
            logger.debug("_send_to_user_sockets: unserializable frame", exc_info=True)
            return 0
        from orchestrator.async_tasks import VirtualWebSocket
        sent = 0
        for ws, claims in list(self.ui_sessions.items()):
            if (claims or {}).get("sub") != user_id:
                continue
            # A background turn's own VirtualWebSocket sits in ui_sessions for
            # the turn's lifetime — "delivering" to it would count as a
            # notified device and silently skip the register_ui catch-up
            # replay for users with no real socket connected.
            if isinstance(ws, VirtualWebSocket):
                continue
            try:
                if await self._safe_send(ws, data):
                    sent += 1
            except Exception:  # pragma: no cover - per-socket best-effort
                logger.debug("_send_to_user_sockets: send failed", exc_info=True)
        return sent

    async def _fan_task_completed(self, bg_task, frame: Dict[str, Any]) -> int:
        """BackgroundTaskManager completion hook (055 bg-continuity): the
        task_completed frame reaches every socket of the user — the
        originating socket may be long gone. Returns the delivered count so
        the manager can persist ``notified``."""
        return await self._send_to_user_sockets(bg_task.user_id, frame)

    def _task_started_frame(self, bg_task) -> Dict[str, Any]:
        """A task_started replay frame for an in-flight background task."""
        return {
            "type": "task_started",
            "payload": {
                "task_id": bg_task.task_id,
                "chat_id": bg_task.chat_id,
                "status": bg_task._canonical_status().value,
                "title": bg_task.title,
                "replay": True,
            },
        }

    async def _replay_user_tasks(self, websocket, user_id: str):
        """055 bg-continuity late-connect catch-up: in-flight tasks replay as
        task_started; completed-but-unnotified rows replay as task_completed
        and are marked notified. Fail-open — never breaks registration."""
        from orchestrator.async_tasks import TaskStatus
        try:
            for t in await self.async_task_manager.list_for_user(user_id):
                if t._canonical_status() not in (
                    TaskStatus.QUEUED,
                    TaskStatus.RUNNING,
                ):
                    continue
                await self._safe_send(websocket, json.dumps(self._task_started_frame(t)))
            rows = await self.history.db.afetch_all(
                "SELECT task_id, chat_id, status, summary, completed_at "
                "FROM background_task WHERE user_id = ? AND notified = FALSE "
                "AND status IN ('completed', 'failed', 'cancelled', 'retryable') "
                "ORDER BY created_at DESC LIMIT 20", (user_id,))
            delivered = []
            for r in rows:
                completed_at = r.get("completed_at")
                sent = await self._safe_send(websocket, json.dumps({
                    "type": "task_completed",
                    "payload": {
                        "task_id": r["task_id"],
                        "chat_id": r["chat_id"],
                        "status": r["status"],
                        "completed_at": completed_at.isoformat() if completed_at else None,
                        "summary": r.get("summary") or "",
                        "replay": True,
                    },
                }))
                if sent:
                    delivered.append(r["task_id"])
            if delivered:
                ph = ", ".join(["?"] * len(delivered))
                await self.history.db.aexecute(
                    f"UPDATE background_task SET notified = TRUE WHERE task_id IN ({ph})",
                    tuple(delivered))
        except Exception:
            logger.debug("background-task replay failed (non-fatal)", exc_info=True)

    async def _replay_chat_task(self, websocket, chat_id: str):
        """055 bg-continuity: a socket joining a chat with a live background
        run shows the running state immediately (processing_async + a
        task_started replay)."""
        try:
            t = await self.async_task_manager.get_active_for_chat(chat_id)
            if t is None:
                return
            await self._safe_send(websocket, json.dumps({
                "type": "chat_status",
                "status": "processing_async",
                "message": f"Query running in background (task {t.task_id})",
            }))
            await self._safe_send(websocket, json.dumps(self._task_started_frame(t)))
        except Exception:
            logger.debug("active-task replay failed (non-fatal)", exc_info=True)

    async def _resume_chat_streams(self, websocket, user_id: str, chat_id: str):
        """Re-attach a socket to a chat's push streams. Shared by load_chat
        and the register_ui session resume (055 bg-continuity).

        001-tool-stream-ui (US3 T054): resume any DORMANT streams for this
        chat. Each resumed stream gets a stream_subscribed reply so the
        frontend re-registers it in pushStreamsRef and starts merging chunks
        again."""
        if self.stream_manager is not None:
            try:
                resumed = await self.stream_manager.resume(
                    websocket, user_id, chat_id,
                )
                for resumed_stream_id, resumed_tool_name in resumed:
                    cfg = self._streamable_tools.get(resumed_tool_name, {})
                    resumed_ack = {
                        "type": "stream_subscribed",
                        "stream_id": resumed_stream_id,
                        "tool_name": resumed_tool_name,
                        "agent_id": cfg.get("agent_id", ""),
                        "session_id": chat_id,
                        "max_fps": cfg.get("max_fps", 30),
                        "min_fps": cfg.get("min_fps", 5),
                        "attached": False,
                    }
                    # 055 US2: keep the resumed placeholder on
                    # its workspace identity (wire-contract §2).
                    resumed_cid = self.stream_manager.component_id_for(
                        resumed_stream_id)
                    if resumed_cid is not None:
                        resumed_ack["component_id"] = resumed_cid
                    await self._safe_send(websocket, json.dumps(resumed_ack))
            except Exception as e:
                logger.warning(f"stream_manager.resume failed: {e}")

        # 055 late-join: resume() above only revives DORMANT
        # streams. Streams kept ACTIVE by the user's other
        # sockets need this socket attached too, plus a
        # replay of the retained chunk so the canvas shows
        # current state instead of a blank placeholder.
        # Rides FF_STREAM_ARTIFACTS — flag off keeps
        # load_chat's frames byte-identical to pre-055.
        if (self.stream_manager is not None
                and flags.is_enabled("stream_artifacts")):
            try:
                attached_subs = await self.stream_manager.attach_to_chat(
                    websocket, user_id, chat_id,
                )
                for att_stream_id, att_tool_name in attached_subs:
                    cfg = self._streamable_tools.get(att_tool_name, {})
                    att_ack = {
                        "type": "stream_subscribed",
                        "stream_id": att_stream_id,
                        "tool_name": att_tool_name,
                        "agent_id": cfg.get("agent_id", ""),
                        "session_id": chat_id,
                        "max_fps": cfg.get("max_fps", 30),
                        "min_fps": cfg.get("min_fps", 5),
                        "attached": True,
                    }
                    att_cid = self.stream_manager.component_id_for(
                        att_stream_id)
                    if att_cid is not None:
                        att_ack["component_id"] = att_cid
                    await self._safe_send(websocket, json.dumps(att_ack))
                    # Ack first: clients key the placeholder
                    # off it before the replay frame fills it.
                    await self.stream_manager.replay_retained(
                        websocket, att_stream_id)
            except Exception as e:
                logger.warning(f"stream_manager.attach_to_chat failed: {e}")

    async def _attach_turn_attachments(self, websocket, message, chat_id, user_id, turn_message_id, attachments):
        """Feature 031: validate, link, and surface this turn's attachments.

        Each staged attachment is ownership-validated; valid ones are linked to
        the persisted user message (``message_attachment``) and listed in a
        structured "Attachments on this turn" block appended to the LLM-facing
        message (with the reader tool that can parse each, or "pending parser").
        Foreign/invalid/deleted references are dropped and audited — never
        parsed. Capped at 10 per turn. Returns the (possibly augmented) message.
        """
        try:
            from orchestrator.attachments.repository import AttachmentRepository
            from orchestrator.attachments.message_attachment_repo import MessageAttachmentRepository
            from orchestrator.attachments.parser_repo import AttachmentParserRepository
            from orchestrator import parser_registry
        except Exception:
            logger.warning("attachment wiring imports failed (non-fatal)", exc_info=True)
            return message

        db = self.history.db
        att_repo = AttachmentRepository(db)
        link_repo = MessageAttachmentRepository(db)
        parser_repo = AttachmentParserRepository(db)

        async def _audit_drop(aid):
            try:
                from datetime import datetime, timezone

                from audit.recorder import get_recorder
                from audit.schemas import AuditEventCreate
                rec = get_recorder()
                if rec is None:
                    return
                # correlation_id and started_at are REQUIRED by AuditEventCreate;
                # omitting them raised a ValidationError that the except below
                # silently swallowed, so cross-user denials were never recorded.
                await rec.record(AuditEventCreate(
                    actor_user_id=user_id or "legacy",
                    auth_principal=user_id or "legacy",
                    event_class="file",
                    action_type="attachment_reference_denied",
                    description=f"Dropped unauthorized/invalid attachment reference {aid}",
                    conversation_id=chat_id,
                    correlation_id=str(_uuid.uuid4()),
                    outcome="failure",
                    started_at=datetime.now(timezone.utc),
                ))
            except Exception:
                logger.warning("attachment drop audit failed", exc_info=True)

        MAX_PER_TURN = 10
        accepted = []
        dropped = 0
        seen = set()
        for entry in (attachments or [])[:50]:
            if len(accepted) >= MAX_PER_TURN:
                break
            aid = entry.get("attachment_id") if isinstance(entry, dict) else None
            if not aid or aid in seen:
                continue
            seen.add(aid)
            att = None
            try:
                att = att_repo.get_by_id(aid, user_id)
            except Exception:
                logger.debug("attachment lookup failed", exc_info=True)
            if att is None:
                dropped += 1
                await _audit_drop(aid)
                continue
            try:
                link_repo.insert(chat_id=chat_id, attachment_id=aid,
                                 user_id=user_id, message_id=turn_message_id)
            except Exception:
                logger.debug("message_attachment insert failed", exc_info=True)
            try:
                cov = parser_registry.coverage(att.extension, att.category, parser_repo=parser_repo)
                readable = cov["tool"] if cov.get("covered") else "pending parser"
            except Exception:
                readable = "unknown"
            accepted.append((att, readable))

        if accepted:
            lines = ["[Attachments on this turn]"]
            for att, readable in accepted:
                lines.append(
                    f'- id={att.attachment_id} name="{att.filename}" '
                    f"category={att.category} (readable: {readable})"
                )
            message = message + "\n\n" + "\n".join(lines)
        if dropped:
            try:
                await self._safe_send(websocket, json.dumps({
                    "type": "chat_status", "status": "info",
                    "message": f"{dropped} attachment(s) couldn't be used and were skipped.",
                }))
            except Exception:
                pass
        return message

    def _defer_voice_chat_dispatch(
        self,
        *,
        voice_dispatch: _VoiceDispatchContext,
        user_id: str,
        chat_id: str,
        stage: Any,
    ) -> bool:
        """Attach one finalizer to the active admitted operation, if present."""

        operation_context = _CONNECTION_OPERATION_CONTEXT.get()
        if (
            not isinstance(operation_context, dict)
            or operation_context.get("operation_kind")
            != "voice_chat_message"
        ):
            return False
        pending = _PendingVoiceFinalization(
            voice_dispatch=voice_dispatch,
            user_id=user_id,
            chat_id=chat_id,
            stage=stage,
        )
        existing = operation_context.get("voice_finalization")
        if existing is not None:
            if (
                isinstance(existing, _PendingVoiceFinalization)
                and existing.voice_dispatch is voice_dispatch
                and existing.stage is stage
                and existing.user_id == user_id
                and existing.chat_id == chat_id
            ):
                return True
            raise RuntimeError("voice finalization was already registered")
        operation_context["voice_finalization"] = pending
        return True

    @staticmethod
    def _remember_voice_operation_terminal_intent(
        task_state: TaskState,
    ) -> bool:
        """Persist a fixed voice outcome even when legacy task tracking is off."""

        operation_context = _CONNECTION_OPERATION_CONTEXT.get()
        if (
            not isinstance(operation_context, dict)
            or operation_context.get("operation_kind")
            != "voice_chat_message"
        ):
            return False
        intents = {
            TaskState.FAILED: _VoiceOperationTerminalIntent(
                state=OperationState.FAILED,
                terminal_code="operation_failed",
                safe_summary=_VOICE_REQUEST_FAILED_MESSAGE,
            ),
            TaskState.CANCELLED: _VoiceOperationTerminalIntent(
                state=OperationState.CANCELLED,
                terminal_code="cancelled_by_user",
                safe_summary=_VOICE_REQUEST_CANCELLED_MESSAGE,
            ),
            TaskState.RETRYABLE: _VoiceOperationTerminalIntent(
                state=OperationState.RETRYABLE,
                terminal_code="disconnected",
                safe_summary=_VOICE_REQUEST_INTERRUPTED_MESSAGE,
                retry_after_ms=1000,
            ),
        }
        intent = intents.get(task_state)
        if intent is None:
            raise ValueError("voice terminal intent must be a terminal task state")
        existing = operation_context.get("voice_terminal_intent")
        if existing is not None:
            if existing == intent:
                return True
            raise RuntimeError("voice terminal intent was already registered")
        operation_context["voice_terminal_intent"] = intent
        return True

    @staticmethod
    def _operation_projection_matches_work(
        work: _ConnectionOperation,
        projection: Any,
        *,
        connection_generation: _uuid.UUID | None,
    ) -> bool:
        """Validate the complete public identity of one operation projection."""

        if not isinstance(projection, (OperationRecord, SafeOperationProjection)):
            return False
        if (
            projection.operation_id != work.operation_id
            or projection.operation_kind != work.frame.operation_kind
            or projection.owner_scope is not work.owner.owner_scope
            or str(projection.chat_id or "") != str(work.frame.chat_id or "")
            or projection.request_generation != work.frame.request_generation
        ):
            return False
        if projection.connection_generation != connection_generation:
            return False
        if isinstance(projection, OperationRecord):
            return (
                projection.owner_user_id == work.owner.owner_user_id
                and projection.connection_scope_id
                == work.owner.connection_scope_id
            )
        return True

    async def _reconcile_pending_voice_operation(
        self,
        operation_context: dict[str, Any] | None,
        context: ConnectionContext,
        work: _ConnectionOperation,
        candidate: Any,
    ) -> Any:
        """Resolve one exact terminal before an accepted voice turn is closed."""

        if (
            not isinstance(operation_context, dict)
            or not isinstance(
                operation_context.get("voice_finalization"),
                _PendingVoiceFinalization,
            )
        ):
            return candidate
        terminal_states = {
            OperationState.COMPLETED,
            OperationState.FAILED,
            OperationState.CANCELLED,
            OperationState.RETRYABLE,
        }
        if (
            self._operation_projection_matches_work(
                work,
                candidate,
                connection_generation=context.connection_generation,
            )
            and candidate.state in terminal_states
        ):
            return candidate
        try:
            projection = await self._call_work_admission(
                self.work_admission.query_operation,
                owner=work.owner,
                operation_id=work.operation_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "voice_terminal_operation_reconcile_failed reason=query_failed",
                exc_info=True,
            )
            return None
        if not self._operation_projection_matches_work(
            work,
            projection,
            connection_generation=context.connection_generation,
        ):
            logger.warning(
                "voice_terminal_operation_reconcile_failed reason=identity_mismatch"
            )
            return None
        if projection.state in terminal_states:
            return projection
        if (
            projection.state is not OperationState.RUNNING
            or not isinstance(work.fence, ExecutionFence)
        ):
            return None
        try:
            current = await self._call_work_admission(
                self.work_admission.assert_current_execution,
                work.fence,
            )
        except StaleExecutionFenceError:
            return None
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "voice_terminal_operation_reconcile_failed "
                "reason=fence_check_failed",
                exc_info=True,
            )
            return None
        if (
            not self._operation_projection_matches_work(
                work,
                current,
                connection_generation=context.connection_generation,
            )
            or current.state is not OperationState.RUNNING
        ):
            return None
        resolved = await self._terminalize_connection_operation(
            context,
            work,
            state=OperationState.FAILED,
            terminal_code="voice_dispatch_incomplete",
            safe_summary=_VOICE_REQUEST_FAILED_MESSAGE,
        )
        if (
            self._operation_projection_matches_work(
                work,
                resolved,
                connection_generation=context.connection_generation,
            )
            and resolved.state in terminal_states
        ):
            return resolved
        return None

    async def _finish_pending_voice_dispatch(
        self,
        operation_context: dict[str, Any] | None,
        terminal_operation: Any,
    ) -> None:
        """Bound terminal reconciliation to this runner's live authority.

        The operation context becomes unreachable when the connection runner
        returns, so transient authority failures are retried inline.  Exhaustion
        leaves the durable turn unchanged rather than inventing success or
        failure, logs the missing terminal announcement, and scrubs the
        runner-local finalizer.
        """

        if not isinstance(operation_context, dict):
            return
        pending = operation_context.get("voice_finalization")
        if not isinstance(pending, _PendingVoiceFinalization):
            return
        operation_state = getattr(terminal_operation, "state", None)
        if not isinstance(operation_state, OperationState) or operation_state not in {
            OperationState.COMPLETED,
            OperationState.FAILED,
            OperationState.CANCELLED,
            OperationState.RETRYABLE,
        }:
            logger.warning(
                "voice_terminal_finalization_reconciling reason=operation_nonterminal"
            )
            terminal_operation = None
        for attempt in range(_VOICE_TERMINAL_FINALIZATION_ATTEMPTS):
            finalized = await self._finish_voice_chat_dispatch(
                voice_dispatch=pending.voice_dispatch,
                user_id=pending.user_id,
                chat_id=pending.chat_id,
                stage=pending.stage,
                operation_context=operation_context,
                operation_projection=terminal_operation,
            )
            if finalized:
                if operation_context.get("voice_finalization") is pending:
                    operation_context.pop("voice_finalization", None)
                return
            if operation_context.get("voice_finalization") is not pending:
                return
            if attempt + 1 < _VOICE_TERMINAL_FINALIZATION_ATTEMPTS:
                # A supplied projection may be stale or belong to a peer.  The
                # retry must query the exact operation authority from the
                # retained context rather than replaying that candidate.
                terminal_operation = None
                await asyncio.sleep(0)
        operation = operation_context.get("operation")
        logger.warning(
            "voice_terminal_finalization_exhausted attempts=%s operation_id=%s",
            _VOICE_TERMINAL_FINALIZATION_ATTEMPTS,
            getattr(operation, "operation_id", None),
        )
        if operation_context.get("voice_finalization") is pending:
            operation_context.pop("voice_finalization", None)

    async def _voice_dispatch_operation_state(
        self,
        *,
        turn: Any,
        user_id: str,
        chat_id: str,
        connection_generation: str,
        operation_context: Any = None,
        terminal_projection: Any = None,
    ) -> tuple[OperationState | None, ExecutionFence | None]:
        """Read the exact shared operation outcome for one committed voice turn.

        The operation record is the deterministic result authority.  Visible
        component text is deliberately excluded because an error card and a
        successful result are both valid committed conversation snapshots.
        """

        authority = self._conversation_authority(
            (
                operation_context
                if operation_context is not None
                else _CONNECTION_OPERATION_CONTEXT.get()
            ),
            None,
        )
        if authority is None:
            logger.warning(
                "voice_terminal_operation_unavailable reason=authority_missing"
            )
            return None, None
        operation, owner, fence = authority
        if (
            owner.owner_scope is not OwnerScope.USER
            or owner.owner_user_id != user_id
            or operation.owner_scope is not owner.owner_scope
            or operation.operation_kind != "voice_chat_message"
            or str(operation.operation_id) != str(turn.operation_id or "")
            or str(operation.chat_id or "") != str(chat_id)
            or str(operation.request_generation or "")
            != str(turn.request_generation)
            or str(operation.connection_generation or "")
            != str(connection_generation)
        ):
            logger.warning(
                "voice_terminal_operation_unavailable reason=identity_mismatch"
            )
            return None, None
        projection = terminal_projection
        if projection is None:
            try:
                projection = await self._call_work_admission(
                    self.work_admission.query_operation,
                    owner=owner,
                    operation_id=operation.operation_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "voice_terminal_operation_unavailable reason=query_failed",
                    exc_info=True,
                )
                return None, None
        if not isinstance(
            projection,
            (OperationRecord, SafeOperationProjection),
        ):
            logger.warning(
                "voice_terminal_operation_unavailable reason=projection_mismatch"
            )
            return None, None
        if (
            projection.operation_id != operation.operation_id
            or projection.operation_kind != operation.operation_kind
            or projection.owner_scope is not owner.owner_scope
            or projection.chat_id != operation.chat_id
            or projection.request_generation != operation.request_generation
            or projection.connection_generation != operation.connection_generation
        ):
            logger.warning(
                "voice_terminal_operation_unavailable reason=projection_mismatch"
            )
            return None, None
        return projection.state, fence

    async def _finish_voice_chat_dispatch(
        self,
        *,
        voice_dispatch: _VoiceDispatchContext,
        user_id: str,
        chat_id: str,
        stage: Any,
        operation_context: Any = None,
        operation_projection: Any = None,
    ) -> bool:
        """Speak one proven terminal outcome.

        Returns ``True`` when no further finalization is required.  ``False``
        means exact terminal authority or durable announcement finalization was
        temporarily unavailable, so a pending caller may retry safely.
        """

        services = getattr(self, "voice_services", None)
        if services is None:
            return False
        initial_turn = voice_dispatch.admission.turn
        try:
            turn = await asyncio.to_thread(
                services.repository.get_turn,
                user_id=user_id,
                turn_id=initial_turn.turn_id,
            )
            # A preflight refusal happens before ordinary message acceptance;
            # it must not fabricate a completion or mutate the recognition
            # disposition here.
            if turn.state not in {"accepted", "processing", "waiting_on_user"}:
                return True
            operation_state, operation_fence = (
                await self._voice_dispatch_operation_state(
                    turn=turn,
                    user_id=user_id,
                    chat_id=chat_id,
                    connection_generation=voice_dispatch.connection_generation,
                    operation_context=operation_context,
                    terminal_projection=operation_projection,
                )
            )
            if operation_state not in {
                OperationState.COMPLETED,
                OperationState.FAILED,
                OperationState.CANCELLED,
                OperationState.RETRYABLE,
            }:
                logger.warning(
                    "voice_terminal_operation_unavailable reason=operation_nonterminal"
                )
                return False
            committed = bool(
                stage is not None and stage.sealed and stage.committed
            )
            exact_committed_stage = bool(
                committed
                and operation_fence is not None
                and isinstance(stage.operation_fence, ExecutionFence)
                and stage.operation_fence == operation_fence
                and str(stage.commit_id) == str(turn.result_commit_id or "")
            )
            if operation_state in {
                OperationState.FAILED,
                OperationState.CANCELLED,
                OperationState.RETRYABLE,
            }:
                cancelled = operation_state is OperationState.CANCELLED
                if cancelled:
                    notice = _VOICE_REQUEST_CANCELLED_MESSAGE
                    spoken_terminal = (
                        "That request was cancelled. No completed result was "
                        "produced."
                    )
                elif operation_state is OperationState.RETRYABLE:
                    notice = _VOICE_REQUEST_INTERRUPTED_MESSAGE
                    spoken_terminal = (
                        "That request was interrupted and did not complete. "
                        "Please try again."
                    )
                else:
                    notice = _VOICE_REQUEST_FAILED_MESSAGE
                    spoken_terminal = (
                        "That request failed and did not complete. Please "
                        "review the error in the conversation, then try again."
                    )
                terminal_turn = await services.finish_turn_announcements(
                    turn,
                    terminal_kind="cancelled" if cancelled else "failed",
                    recap_text=spoken_terminal,
                    recap_source="terminal_status",
                    sensitivity="unknown",
                    result_commit_id=(
                        turn.result_commit_id if exact_committed_stage else None
                    ),
                )
                await self._broadcast_voice_turn_state(
                    terminal_turn,
                    message=notice,
                )
                return True
            if not exact_committed_stage:
                terminal_turn = await services.finish_turn_announcements(
                    turn,
                    terminal_kind="failed",
                    recap_text=(
                        "The request finished processing, but no result could "
                        "be published. Please try again."
                    ),
                    recap_source="terminal_status",
                    sensitivity="unknown",
                    result_commit_id=None,
                )
                await self._broadcast_voice_turn_state(
                    terminal_turn,
                    message=_VOICE_RESULT_UNAVAILABLE_MESSAGE,
                )
                return True

            content = await asyncio.to_thread(
                self.conversation_commits.committed_assistant_content,
                commit_id=stage.commit_id,
                owner_user_id=user_id,
            )
            committed_components: list[dict[str, Any]] = []
            authoritative_summary = stage.summary_text
            if isinstance(content, list):
                committed_components = [
                    item for item in content if isinstance(item, dict)
                ]
            elif isinstance(content, dict):
                committed_components = [content]
            elif isinstance(content, str) and content.strip():
                committed_components = [
                    {"type": "text", "content": content}
                ]
            if authoritative_summary is None:
                from orchestrator.conversation_publication import (
                    completion_summary_from_content,
                )

                completion_summary = completion_summary_from_content(content)
                if completion_summary is not None:
                    authoritative_summary = completion_summary.summary_text

            from orchestrator.voice_recap import (
                apply_sensitivity_policy,
                build_spoken_recap,
            )

            recap = build_spoken_recap(
                authoritative_summary=authoritative_summary,
                committed_components=committed_components,
                detected_language=turn.detected_language or "en",
            )
            sensitive_detail_recap = recap.text
            try:
                from personalization.phi_gate import get_phi_gate

                phi_present = await asyncio.to_thread(
                    get_phi_gate().detect_for_notice,
                    recap.text,
                )
                confidentiality = (
                    "sensitive" if phi_present else "non_sensitive"
                )
            except Exception:
                confidentiality = "unknown"
            recap = apply_sensitivity_policy(
                recap,
                confidentiality=confidentiality,
                contains_phi=lambda _text: confidentiality != "non_sensitive",
            )
            if recap.sensitivity == "sensitive":
                await services.remember_sensitive_recap(
                    turn,
                    result_id=stage.commit_id,
                    text=sensitive_detail_recap,
                )
            terminal_turn = await services.finish_turn_announcements(
                turn,
                terminal_kind="succeeded",
                recap_text=recap.text,
                recap_source=recap.source,
                sensitivity=recap.sensitivity or "unknown",
                result_commit_id=stage.commit_id,
            )
            await self._broadcast_voice_turn_state(
                terminal_turn,
                message=_VOICE_REQUEST_SUCCEEDED_MESSAGE,
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            # The committed text result is authoritative. Speech degradation
            # is observable but must never replace or roll it back.
            logger.warning(
                "Voice terminal recap scheduling was unavailable",
                exc_info=True,
            )
            return False

    async def _deliver_accepted_voice_turn(
        self,
        websocket: Any,
        *,
        voice_dispatch: _VoiceDispatchContext,
        operation_context: Any,
        acceptance_stage: Any,
        acceptance_record: Any,
        accepted_turn: Any,
        accepted_message_id: int,
    ) -> dict[str, Any]:
        """Deliver one committed acceptance before model execution begins."""

        await self._deliver_committed_conversation_snapshot(
            websocket,
            stage=acceptance_stage,
            request_generation=str(
                voice_dispatch.admission.turn.request_generation
            ),
            committed=acceptance_record,
            server_initiated=True,
        )
        services = getattr(self, "voice_services", None)
        if services is None:
            raise RuntimeError("voice services became unavailable")
        try:
            await services.coordinator.emit_transcript_accepted(
                accepted_turn,
                accepted_message_id=accepted_message_id,
            )
        except Exception:
            logger.warning(
                "Voice worker acceptance disposition was unavailable",
                exc_info=True,
            )
        active_context = (
            operation_context
            or _CONNECTION_OPERATION_CONTEXT.get()
            or {}
        )
        operation = active_context.get("operation")
        reconnectable_work = getattr(
            self, "_reconnectable_operations", {}
        ).get(getattr(operation, "operation_id", None))
        await self._broadcast_voice_ack(
            reconnectable_work,
            fallback_websocket=websocket,
            fallback_connection_generation=(
                voice_dispatch.connection_generation
            ),
            turn=accepted_turn,
        )
        await self._broadcast_voice_turn_state(
            accepted_turn,
            message=_VOICE_REQUEST_PROCESSING_MESSAGE,
        )
        try:
            await services.start_turn_announcements(accepted_turn)
        except Exception:
            logger.warning(
                "Voice acknowledgement scheduling was unavailable",
                exc_info=True,
            )
        return {
            "message_id": accepted_message_id,
            "turn": accepted_turn,
        }

    async def handle_chat_message(
        self,
        websocket,
        message: str,
        chat_id: str,
        display_message: str = None,
        user_id: str = None,
        draft_agent_id: str = None,
        selected_tools=None,
        attachments=None,
        operation_context=None,
        voice_dispatch=None,
    ):
        """Run one chat turn inside its complete staged publication scope."""

        from orchestrator.conversation_publication import (
            reset_conversation_publication,
        )
        from orchestrator.scheduled_publication import (
            current_scheduled_history_stage,
        )

        if user_id is None:
            user_id = self._get_user_id(websocket)
        stage = None
        token = None
        request_generation = None
        server_initiated = False
        voice_acceptance = None
        llm_preflight_complete = False
        if (
            current_scheduled_history_stage() is None
            and voice_dispatch is not None
        ):
            try:
                await self._resolve_llm_client_for(websocket)
                llm_preflight_complete = True
            except self._LLMUnavailable:
                # The ordinary preflight below owns the correlated refusal and
                # setup guidance. Nothing has been accepted or persisted yet.
                pass
            if llm_preflight_complete:
                user_content = display_message if display_message else message
                async with self._workspace_mutation_lock(chat_id):
                    (
                        stage,
                        token,
                        request_generation,
                        acceptance_stage,
                        acceptance_record,
                        accepted_turn,
                        accepted_message_id,
                    ) = await self._begin_voice_conversation_publication(
                        websocket,
                        chat_id=chat_id,
                        user_id=user_id,
                        user_content=user_content,
                        operation_context=operation_context,
                        voice_dispatch=voice_dispatch,
                    )
                server_initiated = True
                deferred = False
                try:
                    deferred = self._defer_voice_chat_dispatch(
                        voice_dispatch=voice_dispatch,
                        user_id=user_id,
                        chat_id=chat_id,
                        stage=stage,
                    )
                    voice_acceptance = await self._deliver_accepted_voice_turn(
                        websocket,
                        voice_dispatch=voice_dispatch,
                        operation_context=operation_context,
                        acceptance_stage=acceptance_stage,
                        acceptance_record=acceptance_record,
                        accepted_turn=accepted_turn,
                        accepted_message_id=accepted_message_id,
                    )
                except BaseException:
                    if stage is not None and not stage.sealed:
                        try:
                            await asyncio.to_thread(
                                self.conversation_commits.abort_commit,
                                commit_id=stage.commit_id,
                                owner_user_id=stage.user_id,
                            )
                            stage.seal(committed=False)
                        except Exception:
                            logger.warning(
                                "conversation stage abort failed",
                                exc_info=True,
                            )
                    if token is not None:
                        reset_conversation_publication(token)
                        token = None
                    if not deferred:
                        await self._finish_voice_chat_dispatch(
                            voice_dispatch=voice_dispatch,
                            user_id=user_id,
                            chat_id=chat_id,
                            stage=stage,
                        )
                    raise
        elif current_scheduled_history_stage() is None:
            stage, token, request_generation = (
                await self._begin_conversation_publication(
                    websocket,
                    chat_id=chat_id,
                    user_id=user_id,
                    operation_context=operation_context,
                )
            )
            if stage is None:
                try:
                    chat_identity = _uuid.UUID(str(chat_id))
                except (TypeError, ValueError, AttributeError):
                    chat_identity = None
                if chat_identity is not None and chat_identity.version == 4:
                    request_generation = str(_uuid.uuid4())
                    stage, token = (
                        await self._begin_detached_conversation_publication(
                            chat_id=chat_id,
                            user_id=user_id,
                            request_generation=request_generation,
                        )
                    )
                    server_initiated = True
        try:
            result = await self._handle_chat_message_impl(
                websocket,
                message,
                chat_id,
                display_message,
                user_id=user_id,
                draft_agent_id=draft_agent_id,
                selected_tools=selected_tools,
                attachments=attachments,
                operation_context=operation_context,
                voice_dispatch=voice_dispatch,
                conversation_stage=stage,
                conversation_request_generation=request_generation,
                conversation_server_initiated=server_initiated,
                voice_acceptance=voice_acceptance,
                llm_preflight_complete=llm_preflight_complete,
            )
            if voice_dispatch is not None:
                deferred = self._defer_voice_chat_dispatch(
                    voice_dispatch=voice_dispatch,
                    user_id=user_id,
                    chat_id=chat_id,
                    stage=stage,
                )
                if not deferred:
                    await self._finish_voice_chat_dispatch(
                        voice_dispatch=voice_dispatch,
                        user_id=user_id,
                        chat_id=chat_id,
                        stage=stage,
                    )
            return result
        except BaseException:
            if voice_dispatch is not None:
                deferred = self._defer_voice_chat_dispatch(
                    voice_dispatch=voice_dispatch,
                    user_id=user_id,
                    chat_id=chat_id,
                    stage=stage,
                )
                if not deferred:
                    await self._finish_voice_chat_dispatch(
                        voice_dispatch=voice_dispatch,
                        user_id=user_id,
                        chat_id=chat_id,
                        stage=stage,
                    )
            raise
        finally:
            if stage is not None and not stage.sealed:
                try:
                    await asyncio.to_thread(
                        self.conversation_commits.abort_commit,
                        commit_id=stage.commit_id,
                        owner_user_id=stage.user_id,
                    )
                    stage.seal(committed=False)
                except Exception:
                    logger.warning(
                        "conversation stage abort failed",
                        exc_info=True,
                    )
            if token is not None:
                reset_conversation_publication(token)

    async def _handle_chat_message_impl(
        self,
        websocket,
        message: str,
        chat_id: str,
        display_message: str = None,
        user_id: str = None,
        draft_agent_id: str = None,
        selected_tools=None,
        attachments=None,
        operation_context=None,
        voice_dispatch=None,
        conversation_stage=None,
        conversation_request_generation: str | None = None,
        conversation_server_initiated: bool = False,
        voice_acceptance: dict[str, Any] | None = None,
        llm_preflight_complete: bool = False,
    ):
        """Process a chat message: LLM determines which tools to call (Multi-Turn Re-Act Loop).

        Feature 013 / FR-018, FR-020, FR-023: ``selected_tools`` is the
        user's in-chat tool-picker subset. When not None, the per-turn
        filter loop excludes any tool not in the subset — narrowing only,
        never widening (scope/per-tool permissions are still enforced).

        Feature 031: ``attachments`` is the list of attachment references the
        user staged on this turn ({attachment_id, filename, category}). Each is
        ownership-validated, linked to the persisted message, and surfaced to
        the LLM as a structured "Attachments on this turn" block so it calls the
        right reader tool with the real attachment_id.
        """
        if voice_dispatch is None:
            logger.info(
                "Processing chat message for chat_id %s",
                chat_id,
            )
        else:
            logger.info(
                "Processing proof-verified voice chat message for chat_id %s",
                chat_id,
            )
        from orchestrator.scheduled_publication import (
            current_scheduled_history_stage,
        )

        scheduled_history_stage = current_scheduled_history_stage()
        if user_id is None:
            user_id = self._get_user_id(websocket)
        # Feature 013 defensive: a stray empty selection from the WS
        # payload is treated as "no narrowing" for this single request,
        # logged so operators can see if the UI gate (FR-021) ever leaks.
        if selected_tools is not None and len(selected_tools) == 0:
            logger.warning(
                "Chat dispatch received empty selected_tools (chat_id=%s user_id=%s) "
                "reason=empty_selection_received — treating as no narrowing.",
                chat_id,
                user_id,
            )
            selected_tools = None
        # If the user has not narrowed in the WS payload, fall back to
        # their saved per-user preference (FR-024).
        if selected_tools is None and user_id is not None and not draft_agent_id:
            try:
                # Resolve the chat's bound agent so the saved selection
                # for THAT agent is applied; if the chat is unbound, no
                # agent-specific selection applies and the orchestrator
                # uses its full default.
                def _saved_tool_selection():
                    """Bound-agent saved tool selection, read off the event loop."""
                    bound_agent_id = self.history.db.get_chat_agent(chat_id) if chat_id else None
                    if bound_agent_id is None:
                        return None
                    return self.history.db.get_user_tool_selection(user_id, bound_agent_id)

                saved = await asyncio.to_thread(_saved_tool_selection)
                if saved is not None and len(saved) > 0:
                    selected_tools = saved
            except Exception as e:  # pragma: no cover — defensive
                logger.debug(f"Could not resolve saved tool selection: {e}")
        # Feature 031: an attachments-only turn (no typed text) still proceeds —
        # synthesize a minimal instruction so the LLM engages with the files.
        if (not message) and attachments:
            message = "Please review the attached file(s)."
        if not message:
            logger.warning("Empty message received")
            return

        # Feature 040 (US5): expand a user-typed /slash-command into a normal
        # prompt BEFORE any processing, so the rewritten turn flows through the
        # exact same permission / audit / PHI gates (no privileged bypass). The
        # user still sees their original "/command" via display_message. Pure
        # prompt-shaping; fail-open to today's behavior on any error.
        if flags.is_enabled("slash_commands"):
            try:
                from orchestrator import slash_commands
                _expanded = slash_commands.expand_message(message)
                if _expanded != message:
                    if display_message is None:
                        display_message = message  # preserve what the user typed
                    message = _expanded
            except Exception:
                logger.debug("slash_commands: expansion skipped (fail-open)", exc_info=True)

        # 030 FR-009 (025 T021): intercept deterministic onboarding ParamPicker
        # submits before the LLM/history path and persist them directly. These
        # are fixed templates ("Save my personalization profile — ...") posted by
        # the onboarding panels; nothing interpreted them before, so selections
        # were silently dropped. Handled submits never enter the LLM path.
        if not draft_agent_id:
            try:
                from orchestrator import onboarding_submit
                if onboarding_submit.is_onboarding_submit(message):
                    onboarding_result: Dict[str, str] = {}

                    def _capture_onboarding_result(text: str, variant: str) -> None:
                        onboarding_result.update(text=text, variant=variant)

                    if await onboarding_submit.handle_submit(
                            self, websocket, user_id, message, chat_id,
                            result_sink=_capture_onboarding_result):
                        msg_to_save = display_message if display_message else message
                        if voice_acceptance is None:
                            await self._append_conversation_message(
                                conversation_stage,
                                chat_id=chat_id,
                                user_id=user_id,
                                role="user",
                                content=msg_to_save,
                            )
                        confirmation = onboarding_result.get(
                            "text", "Your onboarding settings were updated."
                        )
                        await self._append_conversation_message(
                            conversation_stage,
                            chat_id=chat_id,
                            user_id=user_id,
                            role="assistant",
                            content=[
                                Alert(
                                    message=confirmation,
                                    variant=onboarding_result.get(
                                        "variant", "success"
                                    ),
                                ).to_dict()
                            ],
                        )
                        if conversation_stage is not None:
                            await self._publish_conversation_snapshot(
                                websocket,
                                stage=conversation_stage,
                                request_generation=(
                                    conversation_request_generation
                                ),
                                server_initiated=(
                                    conversation_server_initiated
                                ),
                            )
                        return
            except Exception:  # pragma: no cover - never block a chat turn
                logger.warning("onboarding submit handling failed (non-fatal)", exc_info=True)

        # Feature 054: pre-flight gate — refuse the turn up-front when the
        # caller's context has no LLM configuration, instead of letting the
        # user wait through the loading state for an inevitable failure.
        # For a user socket this IS the server-authoritative first-run gate
        # (FR-014); for system contexts it is the honest-degradation path.
        # The per-call resolver in _call_llm will also catch this, but
        # exiting early avoids the extra UX latency.
        try:
            if not llm_preflight_complete:
                await self._resolve_llm_client_for(websocket)
        except self._LLMUnavailable:
            if voice_dispatch is not None:
                turn = voice_dispatch.admission.turn
                await self._reject_voice_submission(
                    websocket,
                    user_id=user_id,
                    origin=voice_dispatch.origin,
                    submission_id=str(turn.submission_id),
                    request_generation=str(turn.request_generation),
                    chat_id=str(turn.chat_id),
                    connection_generation=(
                        voice_dispatch.connection_generation
                    ),
                    reason="permission_denied",
                    retry_policy="none",
                )
            actor_user_id, auth_principal = self._llm_audit_principals(websocket)
            await self._record_llm_unconfigured(
                self.audit_recorder,
                actor_user_id=actor_user_id,
                auth_principal=auth_principal,
                feature="chat_dispatch",
            )
            # Device-aware guidance (FR-017): the watch cannot host the
            # setup dialog, so point it at the phone/web; every other
            # client gets the setup-dialog copy. Stale native builds that
            # never rendered the mandatory surface get actionable guidance
            # too ("on the web").
            _guide = "Set up your AI provider to start chatting."
            try:
                _prof = self.rote.get_profile(websocket)
                _dt = getattr(getattr(_prof, "device_type", None), "value", None)
                if _dt == "watch":
                    _guide = ("Set up your AI provider on your phone or the "
                              "web first.")
                elif _dt in ("windows", "android", "ios", "macos"):
                    _guide = ("Set up your AI provider to start chatting — "
                              "open Settings, or configure it on the web.")
            except Exception:
                pass
            await self.send_ui_render(websocket, [
                Alert(message=_guide, variant="error").to_dict()
            ])
            return

        # Send loading state to UI
        await self._safe_send(websocket, json.dumps({
            "type": "chat_status",
            "status": "thinking",
            "message": "Analyzing request and planning actions..."
        }))
        
        # Save User Message to History. If display_message is provided, save that instead.
        msg_to_save = display_message if display_message else message
        if voice_acceptance is not None:
            turn_message_id = int(voice_acceptance["message_id"])
        else:
            turn_message_id = await self._append_conversation_message(
                conversation_stage,
                chat_id=chat_id,
                user_id=user_id,
                role="user",
                content=msg_to_save,
            )

        # Feature 030 — fire-and-forget PHI awareness notice (notify-only,
        # fail-open; persistence/audit posture unchanged).
        if scheduled_history_stage is None:
            try:
                asyncio.create_task(self._notify_phi_if_detected(
                    websocket, chat_id, user_id, msg_to_save))
            except Exception:
                logger.debug("phi notice scheduling failed (non-fatal)", exc_info=True)

        # Feature 014: create a per-turn ChatStepRecorder. The recorder's
        # WebSocket emits and persistence are PHI-redacted at the boundary
        # via shared.phi_redactor (FR-009b). Stored on the orchestrator so
        # the cancel_task handler can flush in-flight steps (FR-020/021)
        # and execute_tool_and_wait can record per-tool lifecycle events.
        if scheduled_history_stage is None:
            try:
                from orchestrator.chat_steps import ChatStepRecorder

                recorder = ChatStepRecorder(
                    db=self.history.db,
                    websocket=websocket,
                    safe_send=self._safe_send,
                    chat_id=chat_id,
                    user_id=user_id or "legacy",
                    turn_message_id=turn_message_id,
                )
                self._chat_recorders[id(websocket)] = recorder
            except Exception:  # pragma: no cover — defensive; never block a turn
                logger.warning("Failed to create ChatStepRecorder", exc_info=True)

        # Feature 014: send the persisted message_id back to the frontend so
        # it can stamp its locally-appended user message and group incoming
        # `chat_step` events under the correct turn (steps' turn_message_id
        # FK matches this id). Without this stamp, the frontend cannot
        # interleave step lines under the right turn in multi-turn chats.
        if turn_message_id is not None and voice_dispatch is None:
            await self._safe_send(websocket, json.dumps({
                "type": "user_message_acked",
                "chat_id": chat_id,
                "message_id": turn_message_id,
            }))

        # Capture File Upload Mapping
        upload_match = re.search(r"I have uploaded (.*?) to the backend at: `(.*?)`" , message)
        if upload_match:
            original_name = upload_match.group(1)
            backend_path = upload_match.group(2)
            logger.info(f"Captured file upload mapping: {original_name} -> {backend_path}")
            await asyncio.to_thread(
                self.history.add_file_mapping, chat_id, original_name, backend_path,
                user_id=user_id)

        # Feature 031: validate/link/surface this turn's structured attachments.
        # Augments the LLM-facing `message` with an "Attachments on this turn"
        # block; the SAVED history message (msg_to_save) stays the user's text.
        if attachments:
            try:
                message = await self._attach_turn_attachments(
                    websocket, message, chat_id, user_id, turn_message_id, attachments)
            except Exception:
                logger.warning("attachment turn-processing failed (non-fatal)", exc_info=True)

        # Async title summarization for new chats
        chat_data = await asyncio.to_thread(self.history.get_chat, chat_id, user_id=user_id)
        if (
            scheduled_history_stage is None
            and chat_data
            and len(chat_data.get("messages", []))
            + (
                1
                if conversation_stage is not None
                and conversation_stage.publication_role != "assistant_result"
                else 0
            )
            == 1
        ):
            asyncio.create_task(
                self.summarize_chat_title(chat_id, msg_to_save, user_id=user_id, websocket=websocket)
            )

        # Feature 052 (FR-019): one permission memo spans the whole turn —
        # the tool-list build below and the execute phase resolve each
        # distinct (user, agent, tool, kind) against the database once.
        # Exited in the turn's finally (and before the draft early-return);
        # the next turn always re-reads, so revocations stay visible.
        from orchestrator.tool_permissions import turn_permission_memo
        _perm_memo = turn_permission_memo()
        _perm_memo.__enter__()

        # Build tool definitions from registered agents
        # Filter by user's per-agent tool permissions (RFC 8693 delegation)
        # Draft test chats: only expose the draft agent's tools
        if draft_agent_id:
            logger.info(f"Draft test chat — filtering tools to agent: {draft_agent_id}")
        else:
            logger.info(f"Building tool definitions from {len(self.agent_cards)} agents...")
        tools_desc = []
        tool_to_agent = {}  # Map LLM-facing function name → agent_id
        # 015-external-ai-agents: when two registered agents expose the
        # same tool name (e.g. classify-1 and forecaster-1 both have
        # `submit_dataset`), we qualify the LLM-facing name with an
        # `{agent_id}__` prefix so the model can pick unambiguously.
        # `tool_to_unqualified` maps the qualified LLM-facing name back
        # to the bare skill id that the owning agent expects to receive
        # over the MCP dispatch boundary. For non-colliding tools the
        # qualified and unqualified names are identical.
        tool_to_unqualified: Dict[str, str] = {}

        # Feature 013 follow-up: resolve this user's per-agent disabled
        # set once so we can skip disabled agents wholesale below. The
        # draft-test path bypasses this — testing your own draft must
        # always work even if you've disabled the live version.
        try:
            disabled_agents = (
                set(await asyncio.to_thread(self.history.db.get_user_disabled_agents, user_id))
                if user_id and not draft_agent_id
                else set()
            )
        except Exception as e:  # pragma: no cover — defensive
            logger.debug(f"Could not resolve user disabled-agent list: {e}")
            disabled_agents = set()

        # Phase A: run the single chat visibility predicate off the event loop.
        # The MCP projection calls the same helper, preventing catalog drift.
        from orchestrator.tool_visibility import eligible_tool_pairs

        def _log_exclusion(agent_id: str, skill_id: Optional[str], reason: str) -> None:
            subject = f"Tool '{skill_id}'" if skill_id else f"Agent '{agent_id}'"
            logger.debug(
                "%s excluded user=%s agent=%s reason=%s",
                subject,
                user_id,
                agent_id,
                reason,
            )

        eligible = await asyncio.to_thread(
            eligible_tool_pairs,
            self,
            user_id,
            disabled_agents=disabled_agents,
            draft_agent_id=draft_agent_id,
            selected_tools=selected_tools,
            log_exclusion=_log_exclusion,
        )

        # Phase B: detect skill-id collisions across the surviving pairs.
        # A skill id owned by >1 distinct agent_id needs qualification so
        # the model can pick a specific provider.
        skill_id_owners: Dict[str, set] = {}
        for agent_id, skill in eligible:
            skill_id_owners.setdefault(skill.id, set()).add(agent_id)
        colliding_skill_ids: set = {
            sid for sid, owners in skill_id_owners.items() if len(owners) > 1
        }
        if colliding_skill_ids:
            logger.info(
                "Tool name collisions detected — qualifying with agent_id prefix: %s",
                sorted(colliding_skill_ids),
            )

        # Phase C: emit one tool definition per eligible pair, qualifying
        # the LLM-facing name when there's a collision.
        for agent_id, skill in eligible:
            if skill.id in colliding_skill_ids:
                # OpenAI function-name grammar is [a-zA-Z0-9_-]{1,64}; our
                # agent_ids use hyphens, our skill ids use underscores,
                # and "__" appears in neither — so it's a safe separator.
                llm_name = f"{agent_id}__{skill.id}"
                desc = f"[Provider: {agent_id}] {skill.description or ''}"
            else:
                llm_name = skill.id
                desc = skill.description

            schema = self._adapt_tool_schema_for_model(
                skill.input_schema or {"type": "object", "properties": {}}
            )
            tool_def = {
                "type": "function",
                "function": {
                    "name": llm_name,
                    "description": desc,
                    "parameters": schema
                }
            }
            tools_desc.append(tool_def)
            tool_to_agent[llm_name] = agent_id
            tool_to_unqualified[llm_name] = skill.id

        # Feature 008-llm-text-only-chat (FR-001/FR-002/FR-010).
        # When zero tools survive the filter stack, fall through to a
        # plain LLM chat (text-only mode) instead of the legacy
        # "No agents connected" warning. Three exclusions:
        #  - draft test chats (FR-010): preserve the existing
        #    draft-diagnostic path so misconfigured drafts surface.
        #  - LLM unavailable: already short-circuited at the top of
        #    handle_chat_message (FR-003).
        # The dispatch loop below already accepts an empty tools list
        # cleanly — _call_llm omits the `tools` kwarg when tools_desc
        # is falsy. We tag the audit/log signal so operators can
        # distinguish text-only fallback turns (FR-009).
        is_text_only = not tools_desc and not draft_agent_id

        # Feature 027 — inject the orchestrator meta-tools (create_capability /
        # extend_agent) so the LLM can act on capability gaps (D1). Excluded:
        # draft-test sessions, text-only turns (feature 008 semantics — the
        # user disabled everything deliberately), and flag-off deployments.
        from orchestrator import agentic_creation
        meta_tools_injected = False
        if agentic_creation.should_inject(draft_agent_id) and not is_text_only:
            for _meta_def in agentic_creation.meta_tool_definitions():
                _meta_name = _meta_def["function"]["name"]
                tools_desc.append(_meta_def)
                tool_to_agent[_meta_name] = agentic_creation.META_AGENT_ID
                tool_to_unqualified[_meta_name] = _meta_name
            meta_tools_injected = True

        # Feature 030 — scheduling from chat: the schedule_recurring_task
        # meta-tool makes the feature-025 scheduler reachable from the
        # conversation (a consent card gates creation). Same exclusions as
        # the 027 meta-tools.
        from orchestrator import scheduling_chat
        scheduler_tool_injected = False
        if scheduling_chat.should_inject(draft_agent_id) and not is_text_only:
            for _sched_def in scheduling_chat.meta_tool_definitions():
                _sched_name = _sched_def["function"]["name"]
                tools_desc.append(_sched_def)
                tool_to_agent[_sched_name] = scheduling_chat.META_AGENT_ID
                tool_to_unqualified[_sched_name] = _sched_name
            scheduler_tool_injected = True

        # 030-finish-soul-integration — cross-session memory from chat: the
        # remember/memory_search/memory_get meta-tools make the feature-025
        # memory store usable on request (passive prompt recall is unchanged).
        # Same exclusions as the 027/030 meta-tools.
        from orchestrator import memory_chat
        memory_tool_injected = False
        if memory_chat.should_inject(draft_agent_id) and not is_text_only:
            for _mem_def in memory_chat.meta_tool_definitions():
                _mem_name = _mem_def["function"]["name"]
                tools_desc.append(_mem_def)
                tool_to_agent[_mem_name] = memory_chat.META_AGENT_ID
                tool_to_unqualified[_mem_name] = _mem_name
            memory_tool_injected = True

        # Feature 039 — desktop codegen download: the offer_desktop_codegen
        # meta-tool surfaces a download card for the Windows coding-agent .exe
        # (GitHub-released, SHA-256 + sigstore verified) when a user asks for
        # code that runs on their machine. Same exclusions as the 027/030 tools.
        from orchestrator import desktop_codegen
        desktop_codegen_injected = False
        if desktop_codegen.should_inject(draft_agent_id) and not is_text_only:
            for _dc_def in desktop_codegen.meta_tool_definitions():
                _dc_name = _dc_def["function"]["name"]
                tools_desc.append(_dc_def)
                tool_to_agent[_dc_name] = desktop_codegen.META_AGENT_ID
                tool_to_unqualified[_dc_name] = _dc_name
            desktop_codegen_injected = True

        # 056 US4 — planning decomposition: the delegate_subtasks meta-tool lets
        # the planner split a broad request into bounded, isolated sub-tasks
        # instead of micro-planning every step itself. Flag-gated
        # (FF_RECURSIVE_DELEGATION); same exclusions as the other meta-tools.
        from orchestrator import subtasks as _subtasks
        if _subtasks.should_inject(draft_agent_id) and not is_text_only:
            for _st_def in _subtasks.meta_tool_definitions():
                _st_name = _st_def["function"]["name"]
                tools_desc.append(_st_def)
                tool_to_agent[_st_name] = _subtasks.META_AGENT_ID
                tool_to_unqualified[_st_name] = _st_name

        if not tools_desc and draft_agent_id:
            _perm_memo.__exit__(None, None, None)
            await self.send_ui_render(websocket, [
                Alert(
                    message=(
                        "Draft agent has no usable tools yet. Configure tools "
                        "and permissions before testing it."
                    ),
                    variant="warning",
                ).to_dict()
            ])
            return
        if is_text_only:
            logger.info(
                "Chat dispatch entering text-only mode "
                f"(chat_id={chat_id} user_id={user_id} tools_attempted=0)"
            )

        task = None
        heartbeat_task = None
        task_terminal_on_exit = None
        task_error_on_exit = None
        active_request_token = None
        try:
            # ------------------------------------------------------------------
            # SYSTEM PROMPT
            # ------------------------------------------------------------------
            # Fetch file mappings for this chat
            file_mappings = await asyncio.to_thread(
                self.history.get_file_mappings, chat_id, user_id=user_id)
            file_context = ""
            if file_mappings:
                file_context = "\nFILES ACCESSED IN THIS CHAT (Original Name -> Backend Path):\n"
                for mapping in file_mappings:
                    file_context += f"- {mapping['original_name']} -> {mapping['backend_path']}\n"
                file_context += "\nIMPORTANT: You MUST use the absolute backend path (right side) when calling tools for these files. Never use just the original filename.\n"

            # Feature 028 (FR-029): the canvas context comes from the SAME
            # workspace state the user sees, keyed by the stable component_id
            # the upsert path matches on — so "update the table" turns
            # actually update the table the user is looking at.
            canvas_saved = (await self.workspace.alive_rows(chat_id, user_id)) if chat_id else []
            canvas_context = ""
            if canvas_saved:
                canvas_context = "\nCOMPONENTS CURRENTLY ON CANVAS:\n"
                for sc in canvas_saved:
                    cd = sc.get("component_data", {})
                    if not isinstance(cd, dict):
                        cd = {}
                    source_tool = cd.get("_source_tool", "unknown")
                    source_agent = cd.get("_source_agent", "unknown")
                    canvas_context += (
                        f"- component_id: {sc.get('component_id') or sc['id']} | Title: {sc['title']} "
                        f"| Type: {sc['component_type']} | Tool: {source_tool} | Agent: {source_agent}\n"
                    )

            # With the flag on, the volatile file/canvas sections are appended
            # LAST so the stable instruction prefix stays KV-cache-friendly;
            # off → byte-identical in-place substitution of the legacy
            # f-string.
            system_prompt = context_engineering.compose_system_prompt(
                CHAT_SYSTEM_TEMPLATE,
                file_context=file_context,
                canvas_context=canvas_context,
                cache_stable=flags.is_enabled("context_engineering"),
            )

            # Feature 008-llm-text-only-chat (FR-006a). When this turn
            # is dispatching with no tools, append the text-only
            # addendum so the LLM (a) does not emit tool calls,
            # (b) does not fabricate tool output, and (c) tells the
            # user to enable an agent for action-style requests.
            if is_text_only:
                system_prompt += TEXT_ONLY_SYSTEM_PROMPT_ADDENDUM

            # Inject knowledge-based routing hints if available
            if flags.is_enabled("knowledge_synthesis") and hasattr(self, 'knowledge_index'):
                routing_hints = self.knowledge_index.get_routing_hints()
                if routing_hints:
                    system_prompt += f"\n{routing_hints}\n"

            # Feature 040 (US4): inject authored skill packs for the agents in
            # play THIS turn only (progressive disclosure — not every agent
            # every turn). Wires the previously-dormant get_techniques_for_agent.
            # Bounded + fail-open: any error leaves the turn unchanged.
            if flags.is_enabled("skill_packs") and hasattr(self, 'knowledge_index'):
                try:
                    from orchestrator import skill_packs
                    _meta = {"__orchestrator__", "__scheduler__", "__memory__",
                             "__desktop_codegen__", "__subtasks__"}
                    _agents_in_play = {a for a in tool_to_agent.values() if a and a not in _meta}
                    _digest = skill_packs.build_skill_digest(self.knowledge_index, _agents_in_play)
                    if _digest:
                        system_prompt += f"\n{_digest}\n"
                except Exception:
                    logger.debug("skill_packs.fallback: injection skipped", exc_info=True)

            # 030 FR-010 (025 T028): populate enabled-skill guidance from the
            # tools actually available to the user this turn. Previously
            # ``personalization_skill_lines`` was never assigned, so the call
            # site below always read None and enabling a skill changed nothing.
            # Meta-tools (orchestrator/scheduler/memory pseudo-agents) are
            # excluded — they are not user "skills".
            personalization_skill_lines: List[str] = []
            _meta_agent_ids = {"__orchestrator__", "__scheduler__", "__memory__", "__subtasks__",
                               "__desktop_codegen__"}
            for _td in tools_desc:
                try:
                    _fn = _td.get("function") or {}
                    _name = _fn.get("name")
                    if not _name or tool_to_agent.get(_name) in _meta_agent_ids:
                        continue
                    _desc = (_fn.get("description") or "").strip().split(". ")[0][:160]
                    personalization_skill_lines.append(
                        f"- {_name}: {_desc}" if _desc else f"- {_name}")
                except Exception:  # pragma: no cover - never block a chat turn
                    continue
            personalization_skill_lines = personalization_skill_lines[:40] or None

            # Feature 025 — append per-user personalization (memory recall, user
            # context, enabled-skill guidance, and personality/"soul"). This is
            # added AFTER the compliance/safety preamble and tool rules so the
            # personality block remains subordinate to them (FR-015). Skill
            # guidance lines are supplied from the eligible tool set computed
            # above. Failures here must never break a chat turn.
            try:
                skill_lines = locals().get("personalization_skill_lines") or None
                personalization_fragment = self.personalization_service.build_prompt_fragment(
                    user_id, skill_lines=skill_lines
                )
                if personalization_fragment:
                    system_prompt += f"\n\n{personalization_fragment}\n"
            except Exception as exc:  # pragma: no cover — never block a chat turn
                logger.warning(f"personalization injection failed (non-fatal): {exc}")

            # Feature 027 — capability-gap guidance accompanies the meta-tools.
            if meta_tools_injected:
                system_prompt += agentic_creation.SYSTEM_PROMPT_ADDENDUM

            # Feature 030 — recurring-work guidance accompanies the
            # scheduling meta-tool (stops the model denying the capability).
            if scheduler_tool_injected:
                system_prompt += scheduling_chat.SYSTEM_PROMPT_ADDENDUM

            # Memory guidance accompanies the memory meta-tools.
            if memory_tool_injected:
                system_prompt += memory_chat.SYSTEM_PROMPT_ADDENDUM

            # Feature 039 — desktop codegen guidance accompanies the
            # offer_desktop_codegen meta-tool.
            if desktop_codegen_injected:
                system_prompt += desktop_codegen.SYSTEM_PROMPT_ADDENDUM

            # Mint one unguessable sentinel for this turn and tell the model
            # that anything wrapped in its markers is untrusted DATA, never
            # instructions. Untrusted (non-digest) tool outputs are wrapped at
            # append time below. No-op when the flag is off.
            datamark_on = flags.is_enabled("datamarking")
            turn_sentinel = datamarking.make_turn_sentinel() if datamark_on else None
            if datamark_on:
                system_prompt += "\n\n" + datamarking.spotlight_system_addendum(turn_sentinel)

            # ------------------------------------------------------------------
            # MULTI-TURN LOOP
            # ------------------------------------------------------------------
            # Fetch recent history
            history_messages = []
            chat_data = await asyncio.to_thread(
                self.history.get_chat, chat_id, user_id=user_id)
            if chat_data and "messages" in chat_data:
                # A 060 user message is staged/invisible and therefore is not
                # present in this committed read. Legacy/scheduled history
                # still includes the just-added message and keeps the bounded
                # compatibility slice.
                raw_history = chat_data["messages"]
                if (
                    conversation_stage is None
                    or conversation_stage.publication_role
                    == "assistant_result"
                ):
                    raw_history = raw_history[:-1]
                for h_msg in raw_history[-10:]:
                    role = h_msg.get("role")
                    content = h_msg.get("content")
                    
                    # If content is UI component list, stringify it or summarize it
                    if isinstance(content, list):
                        # Try to find text content or just stringify the whole thing
                        content_str = json.dumps(content)
                        # Optional: limit size of historical UI components
                        if len(content_str) > 2000:
                            content_str = content_str[:2000] + "... [TRUNCATED]"
                    else:
                        content_str = str(content)
                        
                    history_messages.append({"role": role, "content": content_str})

            messages = [
                {"role": "system", "content": system_prompt},
                *history_messages,
                {"role": "user", "content": message}
            ]

            # Expose the user's request to the per-tool supervisor gate
            # (intent-alignment, C-S5) during this turn's tool dispatch.
            if not hasattr(self, "_active_request"):
                self._active_request = {}
            if chat_id:
                active_request_token = _ACTIVE_REQUEST_TEXT.set(message)
                self._active_request[chat_id] = message
                # 056 (FR-021): a fresh top-level turn gets a fresh global chain
                # budget (lazily re-created on the turn's first chained hop). A
                # sub-task, however, runs on a fresh sub-chat under a budget
                # SLICE of its parent turn that ``subtasks._run_one`` pre-binds
                # here; preserve that slice (a budget WITH a parent) so hops
                # started inside the sub-task debit the parent turn's global
                # ceiling instead of a fresh parentless budget — otherwise each
                # sub-task could independently spend the full hop budget
                # (unbounded fan-out / resource-exhaustion bypass).
                _existing_budget = self._chain_budgets.get(chat_id)
                if _existing_budget is None or _existing_budget.parent is None:
                    self._chain_budgets.pop(chat_id, None)

            # 033 turn coordination (flag-gated + fail-open via turn_hooks):
            # flow tool-budget (C-S1), dual ledger (C-N7), skill recall (C-N10),
            # plan-deviation (C-S12). All no-ops when their flags are off.
            from orchestrator import turn_hooks
            _flow = turn_hooks.flow_pattern(
                message, tool_count=len(tools_desc), has_attachment=bool(attachments))
            _ledger = turn_hooks.new_ledger(message)
            _skill_store = self._skill_store(user_id)
            _matched_skill = turn_hooks.match_skill(_skill_store, message)
            if _matched_skill is not None:
                messages.append({"role": "system", "content": (
                    f"A learned recipe matches this request: {_matched_skill.name} "
                    f"(tools: {', '.join(_matched_skill.tools)}). Prefer replaying "
                    "it when appropriate.")})
            _tool_trace: List[Dict[str, Any]] = []   # successful steps → skill induction
            _tools_used = 0                          # for the flow tool budget
            _plan_tools = None                       # turn-1 tool set = the plan

            MAX_TURNS = 10
            turn_count = 0
            heartbeat_task = await self._start_heartbeat(websocket)
            # 055 US3: every rich component this turn lands on the canvas —
            # feeds the coalesced post-done designer pass for native origins.
            _turn_canvas_components: List[Dict[str, Any]] = []
            designed_turn_marker: Optional[str] = None

            # Denial loop detection: track tools denied by permission checks
            denial_tracker: Dict[str, int] = {}  # tool_name -> denial count
            DENIAL_THRESHOLD = 2  # remove tool from prompt after this many denials

            # Task state machine: create and track this Re-Act execution
            if (
                flags.is_enabled("task_state_machine")
                and scheduled_history_stage is None
            ):
                authority = operation_context
                if authority is None:
                    authority = getattr(websocket, "task", None)
                if isinstance(authority, dict):
                    authority_operation = authority.get("operation")
                    authority_owner = authority.get("owner")
                    authority_fence = authority.get("execution_fence")
                else:
                    authority_operation = getattr(authority, "_operation", None)
                    authority_owner = getattr(authority, "_owner", None)
                    authority_fence = getattr(
                        authority, "_execution_fence", None
                    )
                if not (
                    isinstance(
                        authority_operation,
                        (OperationRecord, SafeOperationProjection),
                    )
                    and isinstance(authority_owner, OperationOwner)
                    and (
                        authority_fence is None
                        or isinstance(authority_fence, ExecutionFence)
                    )
                ):
                    authority_operation = None
                    authority_owner = None
                    authority_fence = None
                task = await self.task_manager.admit_task(
                    chat_id,
                    user_id or "",
                    message=message,
                    operation=authority_operation,
                    owner=authority_owner,
                    execution_fence=authority_fence,
                )

            while turn_count < MAX_TURNS:
                # Check for cancellation
                if self.cancelled_sessions.get(id(websocket)):
                    task_terminal_on_exit = TaskState.CANCELLED
                    logger.info(f"Processing cancelled by user for chat_id {chat_id}")
                    await self.send_ui_render(websocket, [
                        Alert(message="Processing was cancelled.", variant="info").to_dict()
                    ], target="chat")
                    await self._append_conversation_message(
                        conversation_stage,
                        chat_id=chat_id,
                        user_id=user_id,
                        role="assistant",
                        content=[
                            Alert(
                                message="Processing was cancelled.",
                                variant="info",
                            ).to_dict()
                        ],
                    )
                    await self._safe_send(websocket, json.dumps({
                        "type": "chat_status",
                        "status": "done",
                        "message": ""
                    }))
                    return

                turn_count += 1
                logger.info(f"--- Turn {turn_count}/{MAX_TURNS} ---")

                # Message compaction: summarize older turns if context budget
                # exceeded. Feature 054: compaction is a SYSTEM-context helper
                # by explicit owner decision — llm_call(None, ...) inside
                # compact_messages resolves the admin system credential; the
                # model name here is only used for context-window sizing.
                if flags.is_enabled("message_compaction"):
                    _sys_cfg = await self._llm_store.get_system()
                    messages, was_compacted = await compact_messages(
                        messages, getattr(_sys_cfg, "model", None) or "default",
                        self._call_llm
                    )
                    if was_compacted:
                        logger.info("Context compacted before LLM call")

                # In-loop context editing — tombstone stale tool outputs so a
                # long tool-calling loop doesn't pin volatile (often untrusted)
                # text in the window. Fail-open; off by default.
                if flags.is_enabled("context_engineering"):
                    try:
                        messages, _n_edited = context_engineering.edit_context(messages)
                        if _n_edited:
                            logger.info(
                                "Context editing: tombstoned %d stale tool output(s)",
                                _n_edited,
                            )
                    except Exception:  # pragma: no cover - never block a turn
                        logger.debug("context editing failed (non-fatal)", exc_info=True)

                # Call LLM. Feature 008: text-only turns tag the audit
                # event with feature="chat_dispatch_text_only" so
                # operators can distinguish fallback dispatches from
                # tool-augmented ones (FR-009).
                call_feature = "chat_dispatch_text_only" if is_text_only else "tool_dispatch"
                # Feature 030: wrap the (non-streaming, possibly minute-long)
                # LLM call in a visible chat_step phase — the walkthrough
                # measured 30-220 s tool-less turns whose ONLY feedback was a
                # static status line. KIND_PHASE rows persist with the same
                # PHI redaction as tool steps; failures here never block.
                _phase_recorder = self._chat_recorders.get(id(websocket))
                _phase_step_id = None
                if _phase_recorder is not None:
                    try:
                        from orchestrator.chat_steps import KIND_PHASE
                        _phase_step_id = await _phase_recorder.start(
                            KIND_PHASE,
                            "Drafting answer" if is_text_only else
                            ("Planning next step" if turn_count == 1 else "Analyzing results"),
                        )
                    except Exception:
                        logger.debug("phase step start failed (non-fatal)", exc_info=True)
                _stream_token = _NARRATIVE_STREAM_CHAT.set(chat_id)
                try:
                    with perf_span("turn.route", chat=chat_id):
                        llm_msg, usage = await self._call_llm(
                            websocket, messages, tools_desc, feature=call_feature,
                        )
                except Exception:
                    if _phase_recorder is not None and _phase_step_id:
                        try:
                            await _phase_recorder.error(_phase_step_id, "LLM call failed")
                        except Exception:
                            pass
                    raise
                finally:
                    _NARRATIVE_STREAM_CHAT.reset(_stream_token)
                if task:
                    await self.task_manager.assert_current_execution(task)
                if _phase_recorder is not None and _phase_step_id:
                    try:
                        await _phase_recorder.complete(_phase_step_id)
                    except Exception:
                        logger.debug("phase step complete failed (non-fatal)", exc_info=True)
                self._accumulate_usage(chat_id, usage)
                if not llm_msg:
                    task_terminal_on_exit = TaskState.FAILED
                    task_error_on_exit = "LLM returned no response"
                    logger.error("LLM returned None, stopping loop.")
                    await self._safe_send(websocket, json.dumps({
                        "type": "chat_status",
                        "status": "done",
                        "message": ""
                    }))
                    await self.send_ui_render(websocket, [
                        Alert(message="Failed to get a response from the AI model. Please try again.", variant="error").to_dict()
                    ])
                    return

                # Check for reasoning content (DeepSeek, o1, etc.)
                reasoning = getattr(llm_msg, 'reasoning_content', None)
                if reasoning:
                    logger.info(f"LLM returned reasoning content ({len(reasoning)} chars)")
                    reasoning_components = [
                        Collapsible(title="Reasoning", content=[
                            Text(content=reasoning, variant="markdown")
                        ]).to_dict()
                    ]
                    # Chat rail, NOT canvas: a canvas-target ui_render replaces
                    # the whole canvas, wiping this turn's already-delivered
                    # components. Reasoning is conversation commentary and
                    # already re-hydrates to the chat rail on reload
                    # (collapsible is a _TEXT_ONLY_TYPES member).
                    await self.send_ui_render(websocket, reasoning_components, target="chat")
                    await self._append_conversation_message(
                        conversation_stage,
                        chat_id=chat_id,
                        user_id=user_id,
                        role="assistant",
                        content=reasoning_components,
                    )

                # Check if LLM wants to call tools
                if llm_msg.tool_calls:
                    logger.info(f"LLM requested {len(llm_msg.tool_calls)} tool(s)")
                    
                    # Notify UI
                    tool_names = [tc.function.name for tc in llm_msg.tool_calls]
                    if task:
                        await self.task_manager.transition_task(
                            task,
                            TaskState.AWAITING_TOOL,
                            current_tool=", ".join(tool_names),
                            turn_count=turn_count,
                        )
                    await self._safe_send(websocket, json.dumps({
                        "type": "chat_status",
                        "status": "executing",
                        "message": f"Running: {', '.join(tool_names)}..."
                    }))

                    # Add assistant's message (with tool calls) to history
                    messages.append(llm_msg)

                    # Execute tools
                    tool_results = []
                    with perf_span("turn.tools", chat=chat_id):
                        if len(llm_msg.tool_calls) == 1:
                            tc = llm_msg.tool_calls[0]
                            res = await self.execute_single_tool(websocket, tc, tool_to_agent, chat_id, user_id=user_id, tool_to_unqualified=tool_to_unqualified)
                            if res:
                                tool_results.append(res)
                        else:
                            # 033 fan-out (C-N8): an oversized parallel wave is split
                            # into bounded batches run in sequence; otherwise one
                            # wave as before. No-op (single wave) when off / small.
                            _batches = turn_hooks.fanout_batches(list(llm_msg.tool_calls))
                            if _batches:
                                logger.info("fanout chat=%s calls=%d batches=%d",
                                            chat_id, len(llm_msg.tool_calls), len(_batches))
                                for _batch in _batches:
                                    tool_results.extend(await self.execute_parallel_tools(
                                        websocket, _batch, tool_to_agent, chat_id,
                                        user_id=user_id, tool_to_unqualified=tool_to_unqualified))
                            else:
                                res_list = await self.execute_parallel_tools(websocket, llm_msg.tool_calls, tool_to_agent, chat_id, user_id=user_id, tool_to_unqualified=tool_to_unqualified)
                                tool_results.extend(res_list)

                    # 033 — flow budget (C-S1), plan-deviation (C-S12), and the
                    # success trace for skill induction (C-N10). No-op when off.
                    _turn_tools = [tc.function.name for tc in llm_msg.tool_calls]
                    _tools_used += len(_turn_tools)
                    for _i, _tc in enumerate(llm_msg.tool_calls):
                        _r = tool_results[_i] if _i < len(tool_results) else None
                        if _r is not None and not getattr(_r, "error", None):
                            try:
                                _ta = json.loads(_tc.function.arguments) if _tc.function.arguments else {}
                            except Exception:
                                _ta = {}
                            _tool_trace.append({"tool": _tc.function.name, "args": _ta})
                            # MAS payload defense (C-S14): scan the agent's output
                            # for injection markers; log findings. No-op when off.
                            _findings = turn_hooks.scan_payload(
                                getattr(_r, "ui_components", None) or getattr(_r, "content", None))
                            if _findings:
                                logger.warning("mas_defense.scan chat=%s tool=%s findings=%d",
                                               chat_id, _tc.function.name, len(_findings))
                    if _plan_tools is None:
                        _plan_tools = list(_turn_tools)
                    elif turn_hooks.plan_deviation(_plan_tools, _turn_tools) is not None:
                        logger.warning("asi_coverage.deviation chat=%s tools=%s", chat_id, _turn_tools)
                    if turn_hooks.over_tool_budget(_flow, _tools_used):
                        logger.info("flow_patterns budget reached (pattern=%s used=%d)", _flow, _tools_used)
                        messages.append({"role": "system", "content": (
                            "Tool budget for this turn is exhausted — give your best "
                            "final answer now without calling more tools.")})
                        tools_desc = []

                    # Collect tool UI components and tag each (recursively)
                    # with source metadata (module-level _tag_source).
                    tool_ui_components = []
                    for i_tc, res in enumerate(tool_results):
                        if res and res.ui_components and not res.error:
                            tc = llm_msg.tool_calls[i_tc] if i_tc < len(llm_msg.tool_calls) else None
                            t_name = tc.function.name if tc else ""
                            a_id = tool_to_agent.get(t_name, "")
                            t_params: Dict[str, Any] = {}
                            if tc is not None:
                                try:
                                    raw_args = tc.function.arguments
                                    if isinstance(raw_args, str):
                                        t_params = json.loads(raw_args) if raw_args else {}
                                    elif isinstance(raw_args, dict):
                                        t_params = raw_args
                                except (ValueError, TypeError):
                                    t_params = {}
                            corr_id = getattr(res, "correlation_id", None)
                            for comp in res.ui_components:
                                _tag_source(comp, a_id, t_name, tool_params=t_params, correlation_id=corr_id)
                                tool_ui_components.append(comp)

                    if tool_ui_components:
                        # Feature 029: the adaptive designer arranges multi-
                        # component rounds (fail-open to the 028 flat append).
                        ws_ops = await self._deliver_round_components(
                            websocket, tool_ui_components, chat_id, user_id=user_id,
                            user_request=message,
                        )
                        _turn_canvas_components.extend(tool_ui_components)
                        if chat_id:
                            await self._append_conversation_message(
                                conversation_stage,
                                chat_id=chat_id,
                                user_id=user_id,
                                role="assistant",
                                content=tool_ui_components,
                            )
                            if ws_ops:
                                # FR-030: capture the workspace state this turn produced.
                                def _snapshot_tool_turn():
                                    """Persist the turn's workspace snapshot off the event loop."""
                                    try:
                                        self.workspace.snapshot(
                                            chat_id, user_id, cause="turn",
                                            turn_message_id=self.history.get_latest_message_id(chat_id, user_id=user_id),
                                        )
                                    except Exception:
                                        logger.debug("workspace snapshot failed (tool turn)", exc_info=True)

                                await asyncio.to_thread(_snapshot_tool_turn)

                    # Append tool outputs to LLM conversation history. The
                    # LLM-visible text is the two-tier digest (a tool's
                    # `_model_digest` wins; else the existing `_data`/full-result
                    # serialization) — see _tool_result_to_llm_content.
                    for i, tc in enumerate(llm_msg.tool_calls):
                        res = tool_results[i] if i < len(tool_results) else None
                        tool_content = self._tool_result_to_llm_content(res)
                        # Spotlight untrusted tool output. A tool's own
                        # `_model_digest` is tool-authored and trusted; only
                        # raw, non-digest output is wrapped as untrusted data
                        # the model must not obey.
                        if datamark_on and not self._result_has_model_digest(res):
                            tool_content = datamarking.spotlight(
                                tool_content, turn_sentinel,
                                sanitize=self._datamark_sanitize_spans,
                            )
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": tc.function.name,
                            "content": tool_content,
                        })

                    # Denial loop detection: track permission-denied tool results
                    if flags.is_enabled("denial_loop_detection"):
                        for i, tc in enumerate(llm_msg.tool_calls):
                            res = tool_results[i] if i < len(tool_results) else None
                            if res and res.error and "restricted" in res.error.get("message", "").lower():
                                name = tc.function.name
                                denial_tracker[name] = denial_tracker.get(name, 0) + 1
                                if denial_tracker[name] >= DENIAL_THRESHOLD:
                                    logger.info(f"Denial loop: removing '{name}' from tools after {denial_tracker[name]} denials")
                                    tools_desc = [t for t in tools_desc if t["function"]["name"] != name]
                                    # Inject a system hint so the LLM stops trying
                                    messages.append({
                                        "role": "system",
                                        "content": f"IMPORTANT: The tool '{name}' is not available due to permission restrictions. Do NOT attempt to use it again. Find an alternative approach or inform the user."
                                    })
                        # If ALL tools have been removed, break early
                        if not tools_desc:
                            logger.warning("All tools denied — breaking Re-Act loop")
                            await self.send_ui_render(websocket, [
                                Alert(message="All available tools are restricted by your permission settings. Please update your agent permissions.", variant="warning").to_dict()
                            ], target="chat")
                            # 055 US1: this break used to exit the loop without
                            # a terminal chat_status, leaving client loading
                            # states (skeletons) stuck until disconnect.
                            await self._safe_send(websocket, json.dumps({
                                "type": "chat_status",
                                "status": "done",
                                "message": ""
                            }))
                            break

                    # Update task state and track tool calls
                    if task:
                        for tc in llm_msg.tool_calls:
                            task.tool_calls_made.append(tc.function.name)
                        await self.task_manager.transition_task(
                            task, TaskState.RUNNING, current_tool=None
                        )

                    # Loop continues to next turn to let LLM analyze results.
                    # (030: name the writing phase — the walkthrough measured
                    # up to 124 s behind the old static "Analyzing results...")
                    await self._safe_send(websocket, json.dumps({
                        "type": "chat_status",
                        "status": "thinking",
                        "message": "Analyzing results and writing the response..."
                    }))
                
                else:
                    # No tool calls -> Final Response
                    # Strip any tool-call tokens that leaked into the
                    # text response — see _sanitize_text_response. This
                    # defends against open-weight LLMs that emit raw
                    # `<|tool_call|>...` markup even when asked not to,
                    # which is what users see when they disable all
                    # agents and the LLM still tries to invoke a tool.
                    raw_content = llm_msg.content or ""
                    # Inspect the raw markup BEFORE stripping so we can name
                    # the tool the model wanted (DSML / OpenAI-leak / Qwen /
                    # Mistral / etc.) and surface a friendly disabled-tool
                    # alert. See Orchestrator._diagnose_leaked_tool_calls.
                    leak_alerts = await asyncio.to_thread(
                        self._diagnose_leaked_tool_calls, raw_content, user_id, chat_id)
                    content = _sanitize_text_response(raw_content)
                    if content != raw_content.strip():
                        logger.warning(
                            "Stripped leaked tool-call tokens from text response "
                            "(chat_id=%s user_id=%s is_text_only=%s raw_len=%d clean_len=%d alerts=%d)",
                            chat_id, user_id, is_text_only, len(raw_content), len(content), len(leak_alerts),
                        )
                    if not content:
                        content = "I'm not sure how to help with that."

                    # 033 — supervisor drafted-answer review (C-S5): block an
                    # answer that leaks a secret / PHI before it is sent. Skill
                    # induction (C-N10): remember this turn's successful tool
                    # sequence for future replay. Ledger snapshot (C-N7). No-ops
                    # when their flags are off.
                    _ok, _why = turn_hooks.review_answer(content)
                    if not _ok:
                        logger.warning("supervisor.block chat=%s reason=%s", chat_id, _why)
                        content = ("I can't share that response — it may expose "
                                   "sensitive or private information.")
                    turn_hooks.induce_skill(_skill_store, message, _tool_trace)
                    if _ledger is not None and _plan_tools:
                        _ledger.plan = list(_plan_tools)
                        logger.info("ledger chat=%s plan=%s", chat_id, _ledger.plan)

                    # 033 — Mixture-of-Agents panel (C-N9): for a hard pure-
                    # reasoning answer, draft a small candidate panel and let moa
                    # aggregate. Plain text only; no-op when off (default).
                    _plain = not content.lstrip().startswith(("{", "["))
                    if _plain and turn_hooks.should_debate(
                            difficulty=(0.7 if (_tools_used == 0 and len(content) > 400) else 0.2),
                            confidence=0.4):
                        _panel = [("draft", content, float(len(content)))]
                        for _k in range(2):
                            try:
                                _pm, _ = await self._call_llm(websocket, messages, [], feature="moa_panel")
                                _pt = _sanitize_text_response(_pm.content or "") if _pm else ""
                                if _pt:
                                    _panel.append((f"cand{_k}", _pt, float(len(_pt))))
                            except Exception:
                                break
                        _agg = turn_hooks.aggregate_candidates(_panel)
                        if _agg:
                            logger.info("moa.panel chat=%s candidates=%d", chat_id, len(_panel))
                            content = _agg

                    parsed_components = None
                    needs_retry = False
                    error_msg = ""
                    
                    # Heuristic: if it looks like JSON containing a component
                    stripped = content.strip()
                    looks_like_json = stripped.startswith("{") or stripped.startswith("[") or "```json" in content

                    if looks_like_json:
                        raw_json = content
                        if "```json" in content:
                            match = re.search(r'```json\n(.*?)\n```', content, re.DOTALL)
                            if match:
                                raw_json = match.group(1)
                            else:
                                match = re.search(r'```(.*?)```', content, re.DOTALL)
                                if match:
                                    raw_json = match.group(1).strip()
                    else:
                        # Fallback: LLM may have output text before JSON components
                        # Search for a JSON array or object containing a "type" field
                        json_match = re.search(r'(\[[\s\S]*\]|\{[\s\S]*\})\s*$', content)
                        if json_match:
                            raw_json = json_match.group(1)
                            looks_like_json = True
                            logger.info("Extracted trailing JSON from mixed text+JSON response")

                    if looks_like_json:
                        try:
                            # Try to parse and find valid components
                            # Using the same technique as the _combine_components_llm parser
                            # First try to parse directly
                            try:
                                data = json.loads(raw_json)
                            except json.JSONDecodeError:
                                # Strip markdown code fences if present
                                if raw_json.startswith("```"):
                                    raw_json = raw_json.split("\n", 1)[1] if "\n" in raw_json else raw_json[3:]
                                    if raw_json.endswith("```"):
                                        raw_json = raw_json[:-3]
                                    raw_json = raw_json.strip()

                                try:
                                    data = json.loads(raw_json)
                                except json.JSONDecodeError:
                                    # Fallback: regex search for JSON
                                    json_match = re.search(r'(\{[\s\S]*\}|\[[\s\S]*\])', raw_json)
                                    if json_match:
                                        data = json.loads(json_match.group())
                                    else:
                                        raise
                            
                            if isinstance(data, dict):
                                # Unwrap common LLM wrapper patterns:
                                # {"components": [...]}, {"ui_components": [...]}, {"content": [...]}
                                for wrapper_key in ("components", "ui_components", "content"):
                                    if wrapper_key in data and isinstance(data[wrapper_key], list):
                                        inner = data[wrapper_key]
                                        # Verify at least one inner item looks like a component
                                        if any(isinstance(x, dict) and "type" in x for x in inner):
                                            data = inner
                                            break
                                else:
                                    data = [data]
                            
                            valid_components = []
                            if isinstance(data, list):
                                for item in data:
                                    if isinstance(item, dict) and "type" in item:
                                        # Recursively validate component structure so the
                                        # client never sees an unrenderable type. Feature 029
                                        # (FR-020): validate against the renderer registry,
                                        # not a hand-copied subset.
                                        from webrender import allowed_primitive_types
                                        self._validate_component_tree(
                                            item, set(allowed_primitive_types()) | {"chart"}
                                        )
                                        valid_components.append(item)
                            
                            if valid_components:
                                parsed_components = valid_components
                            else:
                                needs_retry = True
                                error_msg = "JSON parsed successfully but no valid UI components found. Each component MUST be an object with at least a 'type' field (e.g., {'type': 'card', 'title': '...', 'content': [...]})."
                                
                        except Exception as e:
                            needs_retry = True
                            error_msg = f"Failed to parse UI components. The output is not valid JSON. Error: {str(e)}. Please respond ONLY with valid JSON, with NO surrounding text or markdown formatting."
                    
                    if needs_retry and turn_count < MAX_TURNS:
                        logger.warning(f"UI component generation failed parsing. Retrying. Error: {error_msg}")
                        messages.append(llm_msg)
                        messages.append({
                            "role": "user",
                            "content": f"SYSTEM RECOVERY ERROR: {error_msg}\nRemember, you MUST output ONLY valid JSON without Markdown formatting, enclosing explanations, or preamble. Return the complete corrected component array."
                        })
                        
                        await self._safe_send(websocket, json.dumps({
                            "type": "chat_status",
                            "status": "thinking",
                            "message": "Fixing formatting errors in UI component..."
                        }))
                        continue
                    
                    logger.info("LLM provided final response. conversation complete.")

                    # Manual span (no reindent of the delivery block): closed
                    # after the turn's transcript write below; an exception
                    # skips the perf line but changes nothing else.
                    _narrative_span = perf_span("turn.narrative", chat=chat_id)
                    _narrative_span.__enter__()

                    final_ops = []
                    if parsed_components:
                        if self._is_text_only_components(parsed_components):
                            # Text-only components -- route to chat panel only.
                            # Persisted history matches what the user sees so a
                            # chat reload re-renders the same alert.
                            response_components = list(leak_alerts) + list(parsed_components)
                            if is_text_only:
                                response_components += self._text_only_cta_components(user_id)
                            await self.send_ui_render(websocket, response_components, target="chat")
                        else:
                            # Rich UI components -- canvas gets the parsed components,
                            # chat gets the leak alerts + a CONCISE narrative (030:
                            # the chat rail is words only; a long/structured
                            # narrative becomes a durable canvas doc card). The
                            # persisted message includes BOTH so reload shows
                            # the canvas + alerts.
                            _tools_ran = bool(task.tool_calls_made) if task else False
                            if chat_id and self._narrative_is_long(content):
                                parsed_components = list(parsed_components) + [
                                    self._narrative_doc_card(chat_id, content)]
                                chat_core = [
                                    Text(content=self._concise_lead(content)).to_dict(),
                                    Text(content="The full write-up is on the canvas.",
                                         variant="caption").to_dict()]
                            else:
                                chat_core = self._chat_narrative(content, chat_id=chat_id)
                            final_ops = await self._send_or_replace_components(
                                websocket, parsed_components, chat_id, user_id=user_id
                            ) or []
                            _turn_canvas_components.extend(parsed_components)
                            chat_summary = (list(leak_alerts) + chat_core
                                            + [self._provenance_caption(_tools_ran)])
                            await self.send_ui_render(websocket, chat_summary, target="chat")
                            # Feature 045: the chat transcript stores the TEXT the
                            # user saw (chat_summary), NOT the rich components —
                            # those persist in the workspace and re-hydrate to the
                            # canvas on reload. Keeps the chat rail words-only and
                            # makes a reloaded transcript match the live one.
                            response_components = list(chat_summary)
                    else:
                        _tools_ran = bool(task.tool_calls_made) if task else False
                        # 030: long/structured narrative (drafts, documents,
                        # anything with headings/tables) is promoted to a
                        # durable canvas card; the chat rail gets a concise
                        # plain-words lead. Short answers stay chat-only.
                        narrative_doc = None
                        if chat_id and self._narrative_is_long(content):
                            narrative_doc = self._narrative_doc_card(chat_id, content)
                            final_ops = await self._send_or_replace_components(
                                websocket, [narrative_doc], chat_id, user_id=user_id) or []
                            _turn_canvas_components.append(narrative_doc)
                            chat_core = [
                                Text(content=self._concise_lead(content)).to_dict(),
                                Text(content="The full write-up is on the canvas.",
                                     variant="caption").to_dict()]
                        else:
                            chat_core = self._chat_narrative(content, chat_id=chat_id)
                        response_components = (list(leak_alerts) + chat_core
                                               + [self._provenance_caption(_tools_ran)])
                        # Feature 030: text-only turns for a never-configured
                        # account get a deterministic enable affordance — not
                        # left to the model's prose (which pointed users at a
                        # panel where the agents were not even visible).
                        if is_text_only:
                            response_components += self._text_only_cta_components(user_id)
                        # Concise text response goes to chat panel
                        await self.send_ui_render(websocket, response_components, target="chat")
                        if narrative_doc is not None:
                            # Persist the doc with the turn so reload shows it.
                            response_components = [narrative_doc] + response_components

                    # Save complete interaction to history.  Its exact message
                    # identity is the stale guard used after atomic publication.
                    final_message_id = await self._append_conversation_message(
                        conversation_stage,
                        chat_id=chat_id,
                        user_id=user_id,
                        role="assistant",
                        content=response_components,
                    )

                    # Feature 028 (FR-030): close the turn with a workspace
                    # snapshot when this turn changed the workspace.
                    if final_ops and chat_id:
                        def _snapshot_final_turn():
                            """Persist the final-turn workspace snapshot off the event loop."""
                            try:
                                self.workspace.snapshot(
                                    chat_id, user_id, cause="turn",
                                    turn_message_id=self.history.get_latest_message_id(chat_id, user_id=user_id),
                                )
                            except Exception:
                                logger.debug("workspace snapshot failed (final turn)", exc_info=True)

                        await asyncio.to_thread(_snapshot_final_turn)

                    _narrative_span.__exit__(None, None, None)

                    # The designed layout must be part of the same atomic 060
                    # publication as the transcript/canvas.  Prepare and persist
                    # it before publication, but retain 055's native wire order
                    # by deferring only the refinement render until after done.
                    designed_turn_marker = await self._design_turn_post_done(
                        websocket,
                        chat_id,
                        user_id,
                        message,
                        _turn_canvas_components,
                        turn_marker=final_message_id,
                    )
                    if conversation_stage is not None:
                        await self._publish_conversation_snapshot(
                            websocket,
                            stage=conversation_stage,
                            request_generation=conversation_request_generation,
                            server_initiated=conversation_server_initiated,
                        )
                        if task:
                            await self.task_manager.refresh_task(task.task_id)
                            task.turn_count = turn_count
                    elif task:
                        await self.task_manager.transition_task(
                            task,
                            TaskState.COMPLETED,
                            turn_count=turn_count,
                        )
                    # Terminal status follows the sole committed-state frame.
                    await self._send_chat_status(websocket, "done")
                    if designed_turn_marker is not None:
                        try:
                            originating = (
                                websocket
                                if self._ws_active_chat.get(id(websocket)) == chat_id
                                else None
                            )
                            await self._push_designed_native_canvas(
                                chat_id,
                                user_id,
                                originating,
                                designed_turn_marker,
                            )
                        except Exception:
                            logger.exception(
                                "ui_designer.post_done_delivery_failed "
                                "chat=%s user=%s — committed snapshot remains authoritative",
                                chat_id,
                                user_id,
                            )
                    return

            # If loop exits without final response — generate LLM summary
            if turn_count >= MAX_TURNS:
                logger.info(f"Max turns ({turn_count}) reached. Generating summary of tool outputs.")
                await self._safe_send(websocket, json.dumps({
                    "type": "chat_status",
                    "status": "thinking",
                    "message": "Generating summary..."
                }))

                summary_components = await self._generate_tool_summary(
                    websocket, messages, chat_id, user_id=user_id
                )
                summary_message_id = turn_message_id
                if summary_components:
                    if conversation_stage is not None:
                        try:
                            from orchestrator.voice_recap import (
                                CommittedVisibleTextExtractor,
                            )

                            summary_text = (
                                CommittedVisibleTextExtractor().extract(
                                    summary_components
                                )
                            )
                            if summary_text:
                                conversation_stage.set_completion_summary(
                                    text=summary_text,
                                    source="generated_tool_summary",
                                )
                                if isinstance(summary_components[0], dict):
                                    summary_components[0] = {
                                        **summary_components[0],
                                        "summary_text": summary_text,
                                        "summary_source": (
                                            "generated_tool_summary"
                                        ),
                                    }
                        except (TypeError, ValueError):
                            logger.debug(
                                "completion summary contract was unavailable",
                                exc_info=True,
                            )
                    # Chat rail, NOT canvas — the summary is words about the
                    # tool results; a canvas render would replace (wipe) the
                    # components those tools just delivered.
                    await self.send_ui_render(websocket, summary_components, target="chat")
                    if chat_id:
                        summary_message_id = await self._append_conversation_message(
                            conversation_stage,
                            chat_id=chat_id,
                            user_id=user_id,
                            role="assistant",
                            content=summary_components,
                        )
                else:
                    # Fallback if LLM summary fails — descriptive, not boilerplate.
                    await self.send_ui_render(websocket, [
                        Card(title="Round results", content=[
                            Text(content="Multiple tool operations were completed. Review the results above for details.", variant="body")
                        ]).to_dict()
                    ], target="chat")

                # The max-turns exit uses the same atomic-layout preparation and
                # post-done native refinement order as the normal completion.
                designed_turn_marker = await self._design_turn_post_done(
                    websocket,
                    chat_id,
                    user_id,
                    message,
                    _turn_canvas_components,
                    turn_marker=summary_message_id,
                )

            # Covers both max-turn completion and early loop exits such as all
            # tools becoming unavailable.  All user-visible/history/workspace
            # effects above land before the durable terminal transition.
            if conversation_stage is not None:
                await self._publish_conversation_snapshot(
                    websocket,
                    stage=conversation_stage,
                    request_generation=conversation_request_generation,
                    server_initiated=conversation_server_initiated,
                )
                if task:
                    await self.task_manager.refresh_task(task.task_id)
                    task.turn_count = turn_count
            elif task:
                await self.task_manager.transition_task(
                    task,
                    TaskState.COMPLETED,
                    turn_count=turn_count,
                )
            await self._send_chat_status(websocket, "done")
            if designed_turn_marker is not None:
                try:
                    originating = (
                        websocket
                        if self._ws_active_chat.get(id(websocket)) == chat_id
                        else None
                    )
                    await self._push_designed_native_canvas(
                        chat_id,
                        user_id,
                        originating,
                        designed_turn_marker,
                    )
                except Exception:
                    logger.exception(
                        "ui_designer.post_done_delivery_failed "
                        "chat=%s user=%s — committed snapshot remains authoritative",
                        chat_id,
                        user_id,
                    )

        except StaleExecutionFenceError:
            logger.warning(
                "Task execution ownership changed for chat_id=%s; aborting stale worker",
                chat_id,
            )
        except websockets.exceptions.ConnectionClosed:
            task_terminal_on_exit = TaskState.RETRYABLE
            logger.warning(f"WebSocket closed during chat processing for chat_id {chat_id} — client likely reconnected")
        except Exception as e:
            if task_terminal_on_exit is None:
                task_terminal_on_exit = TaskState.FAILED
                task_error_on_exit = str(e)
            logger.error(f"LLM routing error: {e}", exc_info=True)
            error_text = str(e)

            # Auto-fix: if this is a draft agent test chat and the error is a bad tool schema,
            # trigger auto-fix so the agent code gets corrected automatically.
            if draft_agent_id and hasattr(self, 'lifecycle_manager') and ("invalid" in error_text.lower() and "schema" in error_text.lower()):
                logger.info(f"Bad tool schema for draft agent {draft_agent_id} — triggering auto-fix")
                try:
                    await self._safe_send(websocket, json.dumps({
                        "type": "chat_status", "status": "fixing",
                        "message": "Invalid tool schema detected — auto-fixing agent code..."
                    }))
                    fixed = await self.lifecycle_manager.auto_fix_tool_error(
                        draft_agent_id, "_schema_validation",
                        f"The agent's TOOL_REGISTRY has invalid input_schema definitions. "
                        f"The LLM API rejected the tool schemas with this error: {error_text}\n"
                        f"Common cause: using 'required': True on individual properties instead of "
                        f"a 'required': ['field1', 'field2'] array at the object level.",
                        websocket
                    )
                    if fixed:
                        await self.send_ui_render(websocket, [
                            Alert(message="Tool schema fixed. Agent restarted — please try your message again.", variant="info").to_dict()
                        ])
                    else:
                        await self.send_ui_render(websocket, [
                            Alert(message="Auto-fix could not resolve the schema issue. Try refining the agent.", variant="warning").to_dict()
                        ])
                    await self._send_chat_status(websocket, "done")
                except Exception as fix_err:
                    logger.warning(f"Auto-fix for schema error failed: {fix_err}")
                    await self._send_chat_status(websocket, "done")
                    await self.send_ui_render(websocket, [
                        Alert(message=f"Tool schema error and auto-fix failed: {error_text}", variant="error", title="Error").to_dict()
                    ])
            else:
                # Clear the 'thinking' spinner so the UI doesn't hang
                await self._send_chat_status(websocket, "done")
                # Show a user-friendly error message
                if "424" in error_text or "Failed Dependency" in error_text or "Repository Not Found" in error_text:
                    error_text = ("The LLM server cannot find the configured model. "
                                  "Open LLM settings, use “Load models” to pick a model "
                                  "your provider actually serves, and save again.")
                elif "502" in error_text or "Bad Gateway" in error_text:
                    error_text = "The AI model returned a 502 Bad Gateway error. It may be overloaded or restarting. Please try again in a moment."
                elif "504" in error_text or "Gateway Time-out" in error_text:
                    error_text = "The AI model timed out. It may be overloaded or still warming up. Please try again in a moment."
                elif "timeout" in error_text.lower():
                    error_text = "Request timed out waiting for the AI model. Please try again."
                await self.send_ui_render(websocket, [
                    Alert(message=error_text, variant="error", title="Error").to_dict()
                ])
        finally:
            if task_terminal_on_exit is not None and voice_dispatch is not None:
                self._remember_voice_operation_terminal_intent(
                    task_terminal_on_exit
                )
            if task_terminal_on_exit is not None and task is not None:
                if task._canonical_state() not in {
                    TaskState.COMPLETED,
                    TaskState.FAILED,
                    TaskState.CANCELLED,
                    TaskState.RETRYABLE,
                }:
                    try:
                        await self.task_manager.transition_task(
                            task,
                            task_terminal_on_exit,
                            error=task_error_on_exit,
                        )
                    except Exception:
                        logger.debug(
                            "task could not be terminalized during turn cleanup",
                            exc_info=True,
                        )
            _perm_memo.__exit__(None, None, None)
            if heartbeat_task is not None:
                heartbeat_task.cancel()
            if active_request_token is not None:
                _ACTIVE_REQUEST_TEXT.reset(active_request_token)

    def _accumulate_usage(self, chat_id: Optional[str], usage):
        """Accumulate LLM token usage for a conversation.

        Args:
            chat_id: Conversation identifier. Skipped if None.
            usage: The ``usage`` object from an OpenAI-compatible response.
        """
        if not usage or not chat_id:
            return
        if chat_id not in self.token_usage:
            self.token_usage[chat_id] = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
        self.token_usage[chat_id]["prompt_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
        self.token_usage[chat_id]["completion_tokens"] += getattr(usage, "completion_tokens", 0) or 0
        self.token_usage[chat_id]["total_tokens"] += getattr(usage, "total_tokens", 0) or 0
        logger.info(
            f"Token usage for chat {chat_id}: {self.token_usage[chat_id]}"
        )

    # ------------------------------------------------------------------
    # Feature 006 — credential resolution helpers
    # ------------------------------------------------------------------

    def _llm_context_user_id(self, websocket) -> Optional[str]:
        """Return the user_id owning ``websocket``'s LLM context, or ``None``
        for a SYSTEM context.

        System contexts (feature 054, FR-019): ``websocket is None``
        (background jobs, compaction, combine/condense, narration) and
        scheduled-turn ``VirtualWebSocket``s — those run a user's chat turn
        but bill the admin-managed system credential by explicit owner
        decision, never the owner's own record.
        """
        if websocket is None:
            return None
        from orchestrator.async_tasks import VirtualWebSocket
        if isinstance(websocket, VirtualWebSocket):
            # SYSTEM context by default (scheduled jobs, compaction, background
            # work — 054 FR-019). The exception is a foreground sub-task the
            # user is actively waiting on (subtasks._run_one), which stamps
            # ``llm_context_user_id`` so its ReAct planning and LLM-tool
            # credential resolve to the REQUESTING user, not the admin system
            # account — otherwise a fully-configured user whose deployment has
            # no system credential gets every decomposition failing, and where
            # a system credential exists the user is silently billed to it.
            return getattr(websocket, "llm_context_user_id", None)
        # A live socket absent from ui_sessions has no user claims yet; the
        # _registered_events gate guarantees no LLM dispatch happens before
        # registration, so treating it as SYSTEM here can only surface as an
        # LLMUnavailable refusal, never a credential borrow.
        claims = self.ui_sessions.get(websocket) or {}
        return claims.get("sub")

    async def llm_configured_for(self, user_id: str) -> bool:
        """The first-run gate predicate: True iff ``user_id`` has a
        decryptable persisted LLM configuration (spec FR-013/FR-014)."""
        if not user_id:
            return False
        cfg = await self._llm_store.get(user_id)
        await self._drain_llm_discard_notes()
        return cfg is not None

    async def _drain_llm_discard_notes(self) -> None:
        """Audit any undecryptable-record discards queued by the store
        (FR-010: key rotation/corruption ⇒ audited discard + re-gate)."""
        while True:
            note = self._llm_store.pop_discard_note()
            if note is None:
                return
            scope, discard_id = note
            try:
                from llm_config.audit_events import record_llm_config_change
                await record_llm_config_change(
                    self.audit_recorder,
                    actor_user_id=discard_id if scope == "user" else "system",
                    auth_principal="system",
                    action="discarded_undecryptable",
                    base_url=None,
                    model=None,
                    transport="ws",
                    scope=scope,
                )
            except Exception:  # pragma: no cover — audit is best-effort
                logger.warning("discarded_undecryptable audit failed", exc_info=True)

    async def _resolve_llm_client_for(self, websocket):
        """Resolve the (client, source, resolved) tuple for a per-call LLM
        invocation (feature 054-byo-llm-setup).

        - A live user socket resolves the caller's PERSISTED configuration
          (``user_llm_config`` by the socket's ``sub`` claim) —
          CredentialSource.USER. Absent ⇒ LLMUnavailable: the mandatory
          first-run gate.
        - ``websocket=None`` and scheduled-turn ``VirtualWebSocket``s
          resolve the admin-managed SYSTEM record — CredentialSource.SYSTEM.
          Absent ⇒ LLMUnavailable: background features degrade honestly.

        There is NO fallback in either direction, and no path resolves
        another user's record (FR-019/FR-007).
        """
        user_id = self._llm_context_user_id(websocket)
        if user_id is None:
            config = await self._llm_store.get_system()
            source = self._CredentialSource.SYSTEM
        else:
            config = await self._llm_store.get(user_id)
            source = self._CredentialSource.USER
        await self._drain_llm_discard_notes()
        return self._build_llm_client(config, source)

    def _llm_audit_principals(self, websocket):
        """Return ``(actor_user_id, auth_principal)`` for audit-event emission
        on behalf of ``websocket``.

        Mirrors the convention used elsewhere in the orchestrator
        (handle_ui_message lines 743/758/771). Background-job calls with
        websocket=None get ``actor_user_id='system'`` per FR-011 wiring
        in audit-events.md.
        """
        if websocket is None:
            return ("system", "system")
        claims = self.ui_sessions.get(websocket) or {}
        actor_user_id = claims.get("sub") or "legacy"
        auth_principal = (
            claims.get("preferred_username") or claims.get("sub") or "unknown"
        )
        return (actor_user_id, auth_principal)

    @staticmethod
    def _safe_llm_error_metadata(exc: BaseException) -> _SafeLLMErrorMetadata:
        """Return content-free error facts and the centralized retry decision.

        Provider exception messages can contain response bodies.  This helper
        therefore inspects only the Python exception class hierarchy and the
        SDK's numeric ``status_code`` attribute; callers may safely put the
        returned values in structured logs.  The audit classification remains
        the existing feature-006 enum.
        """

        raw_status = getattr(exc, "status_code", None)
        if raw_status is None:
            raw_status = getattr(getattr(exc, "response", None), "status_code", None)
        status_code = (
            raw_status
            if isinstance(raw_status, int)
            and not isinstance(raw_status, bool)
            and 100 <= raw_status <= 599
            else None
        )

        class_names = {
            base.__name__ for base in type(exc).__mro__
            if isinstance(getattr(base, "__name__", None), str)
        }
        exception_class = type(exc).__name__[:80] or "Exception"
        transport_classes = {
            "APIConnectionError",
            "APITimeoutError",
            "ConnectError",
            "ConnectTimeout",
            "ConnectionError",
            "NetworkError",
            "PoolTimeout",
            "ReadError",
            "ReadTimeout",
            "RemoteProtocolError",
            "TimeoutError",
            "TimeoutException",
            "WriteError",
            "WriteTimeout",
        }
        is_transport = (
            isinstance(exc, (ConnectionError, TimeoutError))
            or bool(class_names & transport_classes)
        )
        is_html_maintenance = isinstance(exc, _LLMHTMLMaintenanceError)

        if status_code in {401, 403}:
            upstream_error_class = "auth_failed"
        elif status_code in {404, 424}:
            upstream_error_class = "model_not_found"
        elif status_code == 429:
            upstream_error_class = "rate_limit"
        elif is_transport:
            upstream_error_class = "transport_error"
        else:
            upstream_error_class = "other"

        retryable_status = (
            status_code in {408, 409, 425, 429}
            or (
                status_code is not None
                and 500 <= status_code <= 599
            )
        )
        return _SafeLLMErrorMetadata(
            exception_class=exception_class,
            status_code=status_code,
            upstream_error_class=upstream_error_class,
            retryable=(is_html_maintenance or is_transport or retryable_status),
        )

    @classmethod
    def _classify_llm_upstream_error(cls, exc: BaseException) -> str:
        """Map a provider exception to the existing audit-event enum."""

        return cls._safe_llm_error_metadata(exc).upstream_error_class

    async def _call_llm(self, websocket, messages, tools_desc=None, temperature=None,
                        feature: str = "tool_dispatch", response_format=None,
                        reasoning_effort=None, allow_stream: bool = False,
                        stream_chat_id: Optional[str] = None):
        """Helper to call LLM with retries and exponential backoff.

        Feature 052 (FR-015): when the caller opts in — ``allow_stream=True``
        or an active ``_NARRATIVE_STREAM_CHAT`` context (how the chat loop's
        route call opts in without changing this signature) — and
        ``FF_LLM_STREAMING`` is on (default), the call runs with
        ``stream=True`` — chunks buffer until the first discriminating delta;
        a prose narrative streams to ``websocket`` as ``ui_stream_data``
        frames scoped to ``stream_chat_id``, a tool-call round emits no
        frames, and any streaming error silently retries non-streaming
        (contracts/narrative-streaming.md). The returned ``(message, usage)``
        is equivalent to the non-streamed shape either way.

        Retries only proven transient failures (408/409/425/429/5xx,
        connection/timeout exceptions, and the internal HTML-maintenance
        marker). All other 4xx, malformed responses, and unknown exceptions
        fail after one attempt. The credential client disables SDK-owned
        retries so this loop is the sole retry budget.

        Optional enhancement params, both probe-and-fallback so a plainer
        OpenAI-compatible endpoint is never broken by them:

        * ``response_format`` (enforced structured output): a
          ``response_format`` value (e.g. ``{"type": "json_object"}`` or a
          ``json_schema`` block) passed straight through to the endpoint.
        * ``reasoning_effort`` (reasoning-budget knob): ``"minimal"`` /
          ``"low"`` / ``"medium"`` / ``"high"``; falls back to the
          ``LLM_REASONING_EFFORT`` global default when the caller passes None.

        If the endpoint rejects either param (400 / unsupported / unknown
        keyword), it is recorded as unsupported for this (base_url, model) and
        the call is retried without it — the request still succeeds, just
        without the enhancement. Subsequent calls skip the rejected param
        entirely.

        Credential resolution happens here. The caller's credentials (or
        operator default) are picked up from the per-WebSocket credential
        store via ``_resolve_llm_client_for``.
        Every call emits an ``llm_call`` audit event with
        ``credential_source``;
        ``LLMUnavailable`` (no credentials anywhere) emits
        ``llm_unconfigured`` instead and returns ``(None, None)``.

        Returns:
            Tuple of (message, usage) where usage is the token usage object
            from the API response, or (None, None) on complete failure.
        """
        actor_user_id, auth_principal = self._llm_audit_principals(websocket)
        try:
            client, source, resolved = await self._resolve_llm_client_for(websocket)
        except self._LLMUnavailable:
            await self._record_llm_unconfigured(
                self.audit_recorder,
                actor_user_id=actor_user_id,
                auth_principal=auth_principal,
                feature=feature,
            )
            return None, None
        # The resolved.model is the user's chosen model when source=USER,
        # else the admin system record's model (source=SYSTEM).
        call_model = resolved.model
        # Assemble the optional enhancement params, minus any this endpoint
        # already told us it doesn't support (probe cache). The in-loop except
        # strips any that draw a fresh rejection.
        cap_key = (getattr(resolved, "base_url", None), call_model)
        unsupported = getattr(self, "_llm_unsupported_params", {}).get(cap_key, set())
        effort = reasoning_effort if reasoning_effort is not None else getattr(
            self, "llm_reasoning_effort", None)
        effort = self._valid_reasoning_effort(effort)
        extra_kwargs: Dict[str, Any] = {}
        if response_format is not None and "response_format" not in unsupported:
            extra_kwargs["response_format"] = response_format
        if effort is not None and "reasoning_effort" not in unsupported:
            extra_kwargs["reasoning_effort"] = effort
        # Device-capability-aware model router. Cheap-first — pick the cheapest
        # tier that fits this task, capped by the connecting device; a
        # low-confidence response escalates one tier (below). Flag-gated
        # (default OFF) + fail-open: with the flag off, or no MODEL_TIERS
        # configured, call_model is the already-resolved default, unchanged.
        #
        # A USER record is different: its endpoint/key/model triple was tested
        # together and the selected model is the user's persisted contract
        # (054 FR-008). Operator MODEL_TIERS must never silently replace that
        # model. The router may still surface the on-device eligibility hint,
        # but tier selection/escalation applies only to the operator-managed
        # SYSTEM credential.
        _route_tier: Optional[int] = None
        escalated = False
        if model_router.router_enabled():
            try:
                prof = self.rote.get_profile(websocket) if websocket is not None else None
                dtype = prof.device_type.value if prof is not None else None
                dec = model_router.route(
                    feature, default_model=call_model, device_type=dtype,
                    device_caps=getattr(prof, "capabilities", None))
                if source != self._CredentialSource.USER:
                    call_model, _route_tier = dec.model, dec.tier
                # On-device lane (C-D6): record whether this turn could run on a
                # capable client's local model, so the client/operator can offload.
                self._last_route_ondevice = bool(dec.ondevice)
                if dec.ondevice:
                    logger.info("model_router: on-device eligible (feature=%s tier=%s)",
                                feature, model_router.tier_name(dec.tier))
            except Exception:
                logger.debug("model_router: selection failed — using default model",
                             exc_info=True)
        if not allow_stream and _NARRATIVE_STREAM_CHAT.get() is not None:
            allow_stream = True
            stream_chat_id = _NARRATIVE_STREAM_CHAT.get()
        stream_allowed = (allow_stream and websocket is not None
                          and self._llm_streaming_enabled())
        attempt = 0
        while attempt < self.MAX_RETRIES:
            attempt += 1
            try:
                kwargs = {
                    "model": call_model,
                    "messages": messages
                }
                if tools_desc:
                    kwargs["tools"] = tools_desc
                    kwargs["tool_choice"] = "auto"
                if temperature is not None:
                    kwargs["temperature"] = temperature
                kwargs.update(extra_kwargs)

                response = None
                if stream_allowed:
                    try:
                        response = await self._call_llm_streamed(
                            websocket, client, kwargs, stream_chat_id)
                    except Exception as _stream_exc:
                        stream_error = self._safe_llm_error_metadata(_stream_exc)
                        logger.warning(
                            "LLM streaming failed exception_class=%s "
                            "status_code=%s upstream_error_class=%s "
                            "retryable=%s; falling back to non-streaming for "
                            "this call",
                            stream_error.exception_class,
                            stream_error.status_code,
                            stream_error.upstream_error_class,
                            stream_error.retryable,
                        )
                        stream_allowed = False
                if response is None:
                    response = await asyncio.to_thread(
                        client.chat.completions.create,
                        **kwargs
                    )
                choices = getattr(response, "choices", None)
                if not choices:
                    raise _LLMMalformedResponseError()
                _msg = getattr(choices[0], "message", None)
                if _msg is None:
                    raise _LLMMalformedResponseError()
                # Defensive: some upstream proxies return a 200 status with
                # an HTML maintenance page body (e.g. an Apache 503/502 from
                # an in-front load balancer that swallowed the upstream
                # error). The OpenAI client happily passes this through as
                # message content, and it would render as the assistant's
                # reply. Detect the shape and treat it as a transient
                # failure so the existing retry + clean-Alert path runs.
                _content = (getattr(_msg, "content", None) or "").lstrip()
                if _content and _content[:200].lower().startswith(
                    ("<!doctype html", "<html", "<head", "<body")
                ):
                    raise _LLMHTMLMaintenanceError()
                # Some serving stacks leak Harmony channel tokens
                # ("<|channel|>thought…") or <think> blocks into content;
                # strip them before any consumer renders or persists it.
                if _msg is not None and isinstance(getattr(_msg, "content", None), str):
                    _msg.content = strip_reasoning_markup(_msg.content)
                usage = getattr(response, "usage", None)
                # Audit: successful llm_call
                total_tokens = getattr(usage, "total_tokens", None) if usage else None
                await self._record_llm_call(
                    self.audit_recorder,
                    actor_user_id=actor_user_id,
                    auth_principal=auth_principal,
                    feature=feature,
                    credential_source=source,
                    resolved=resolved,
                    total_tokens=total_tokens,
                    outcome="success",
                )
                # Feature 006: emit llm_usage_report WS message ONLY when
                # the call was served with the user's personal credentials
                # (FR-016 — operator-default calls are NOT attributed to
                # the user's per-device counters).
                if source == self._CredentialSource.USER and websocket is not None:
                    await self._emit_llm_usage_report(
                        websocket,
                        feature=feature,
                        model=call_model,
                        usage=usage,
                        outcome="success",
                    )
                # Cheap-first cascade — if the router placed this call on a
                # lower tier and the response reads low-confidence
                # (hedge/refusal/empty), escalate ONE tier and re-issue once.
                # Only for prose turns (tool-call turns aren't graded this
                # way). Bounded by ``escalated`` so it happens at most once.
                if (_route_tier is not None and not escalated and not tools_desc
                        and model_router.router_enabled()
                        and not model_router.confidence_ok(getattr(_msg, "content", None))):
                    _next = model_router.escalate(_route_tier)
                    _next_model = (model_router.resolve_model(_next, resolved.model)
                                   if _next is not None else None)
                    if _next_model and _next_model != call_model:
                        escalated, _route_tier, call_model = True, _next, _next_model
                        logger.info("model_router: low-confidence → escalating to "
                                    "tier %s (%s)", model_router.tier_name(_next),
                                    _next_model)
                        attempt -= 1  # the escalation re-call isn't a retry
                        continue
                return _msg, usage
            except Exception as e:
                # Did the endpoint reject one of our optional enhancement
                # params? If so, remember it for this (base_url, model), strip
                # it, and retry immediately — the request itself is fine, just
                # without the enhancement.
                # The raw provider string is inspected only in memory for this
                # compatibility decision; it is never logged or audited.
                drop = (
                    self._llm_unsupported_extras(str(e), extra_kwargs)
                    if extra_kwargs
                    else set()
                )
                if drop:
                    cache = getattr(self, "_llm_unsupported_params", None)
                    if cache is not None:
                        cache.setdefault(cap_key, set()).update(drop)
                    for p in drop:
                        extra_kwargs.pop(p, None)
                    logger.info(
                        "LLM endpoint rejected %s; retrying without it", sorted(drop)
                    )
                    # A capability-probe rejection is not a real failure —
                    # don't spend a retry attempt on it.
                    attempt -= 1
                    continue

                error = self._safe_llm_error_metadata(e)
                logger.warning(
                    "LLM call failed attempt=%d/%d exception_class=%s "
                    "status_code=%s upstream_error_class=%s retryable=%s",
                    attempt,
                    self.MAX_RETRIES,
                    error.exception_class,
                    error.status_code,
                    error.upstream_error_class,
                    error.retryable,
                )

                if not error.retryable or attempt == self.MAX_RETRIES:
                    await self._record_llm_call(
                        self.audit_recorder,
                        actor_user_id=actor_user_id,
                        auth_principal=auth_principal,
                        feature=feature,
                        credential_source=source,
                        resolved=resolved,
                        total_tokens=None,
                        outcome="failure",
                        upstream_error_class=error.upstream_error_class,
                    )
                    if source == self._CredentialSource.USER and websocket is not None:
                        await self._emit_llm_usage_report(
                            websocket, feature=feature, model=call_model,
                            usage=None, outcome="failure",
                        )
                    # Return (None, None) so callers fall through to the
                    # existing user-friendly "Failed to get a response from
                    # the AI model" Alert. Raising here would surface raw
                    # upstream payloads (e.g. a provider's 503 HTML page)
                    # in chat error text. The structured log and audit event
                    # retain only the safe class/status/category facts.
                    return None, None

                # Exponential backoff: 1s, 2s, 4s, 8s with ±20% jitter to
                # avoid thundering-herd when concurrent LLM calls fail against
                # the same upstream (mirrors stream_manager.compute_backoff).
                backoff = min(2 ** (attempt - 1), 8) * random.uniform(0.8, 1.2)
                logger.info(
                    "Transient LLM error; retrying attempt=%d/%d "
                    "backoff_seconds=%.3f",
                    attempt,
                    self.MAX_RETRIES,
                    backoff,
                )
                await asyncio.sleep(backoff)
        # Defensive: unreachable for a positive MAX_RETRIES value.
        return None, None

    @staticmethod
    def _llm_streaming_enabled() -> bool:
        """FF_LLM_STREAMING kill switch (default on), env-read per call so
        operators and tests can flip it without a restart."""
        return os.getenv("FF_LLM_STREAMING", "true").lower() in ("true", "1", "yes")

    async def _call_llm_streamed(self, websocket, client, kwargs, chat_id):
        """One ``stream=True`` completion with buffer-until-discriminate delivery.

        Runs the provider's sync stream in a worker thread, marshaling chunks
        onto the event loop. The first meaningful delta decides the mode:
        ``tool_calls`` ⇒ consume silently and return a normal tool-call
        response (no UI frames); ``content`` ⇒ progressively emit the
        narrative as ``ui_stream_data`` frames scoped to ``chat_id``
        (JSON/fence-shaped output stays silent — it is a component payload the
        final render must deliver whole). Frames are held back to the last
        safe markdown boundary so none ships a dangling ``**``/``*``/backtick/
        ``[`` token (055 FR-013); end-of-stream flushes the full text before
        the terminal clear. Returns a response object shaped
        like the non-streamed one; any failure propagates so the caller
        retries the call non-streaming (contracts/narrative-streaming.md).
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def _pump():
            """Iterate the sync provider stream and feed chunks to the loop."""
            try:
                for chunk in client.chat.completions.create(stream=True, **kwargs):
                    loop.call_soon_threadsafe(queue.put_nowait, ("chunk", chunk))
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))
            except Exception as exc:
                loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))

        pump_task = asyncio.create_task(asyncio.to_thread(_pump))
        content_parts: List[str] = []
        tool_calls_acc: Dict[int, Dict[str, Any]] = {}
        usage = None
        finish_reason = None
        mode = None
        stream_id = "narrative-" + _uuid.uuid4().hex[:12]
        seq = 0
        emitted = False
        sent_len = 0
        last_emit = 0.0
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "done":
                    break
                if kind == "error":
                    raise payload
                chunk = payload
                if getattr(chunk, "usage", None) is not None:
                    usage = chunk.usage
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                choice = choices[0]
                if getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                if getattr(delta, "tool_calls", None):
                    if mode is None:
                        mode = "tool"
                    for tc in delta.tool_calls:
                        idx = getattr(tc, "index", 0) or 0
                        acc = tool_calls_acc.setdefault(
                            idx, {"id": None, "name": "", "arguments": ""})
                        if getattr(tc, "id", None):
                            acc["id"] = tc.id
                        fn = getattr(tc, "function", None)
                        if fn is not None:
                            if getattr(fn, "name", None):
                                acc["name"] += fn.name
                            if getattr(fn, "arguments", None):
                                acc["arguments"] += fn.arguments
                d_content = getattr(delta, "content", None)
                if d_content:
                    content_parts.append(d_content)
                    if mode is None:
                        head = "".join(content_parts).lstrip()
                        if head:
                            mode = "silent" if head[0] in "{[`" else "content"
                    if mode == "content" and (
                            not emitted or time.monotonic() - last_emit >= 0.15):
                        cumulative = "".join(content_parts)
                        safe_len = markdown_safe_prefix_len(cumulative)
                        if safe_len > sent_len:
                            await self._emit_narrative_frame(
                                websocket, chat_id, stream_id, seq,
                                cumulative[:safe_len], terminal=False)
                            seq += 1
                            emitted = True
                            sent_len = safe_len
                            last_emit = time.monotonic()
            if mode == "content":
                # Terminal flush: ship the tail held past the last safe
                # boundary before the clearing frame (055 FR-013).
                full = "".join(content_parts)
                if len(full) > sent_len:
                    await self._emit_narrative_frame(
                        websocket, chat_id, stream_id, seq, full, terminal=False)
                    seq += 1
                    emitted = True
            if emitted:
                await self._emit_narrative_frame(
                    websocket, chat_id, stream_id, seq, "", terminal=True)
        except Exception:
            # Clear any partial streamed text so the non-streaming retry's
            # final render is not visually duplicated; then let the caller
            # fall back.
            if emitted:
                try:
                    await self._emit_narrative_frame(
                        websocket, chat_id, stream_id, seq, "", terminal=True)
                except Exception:
                    logger.debug("narrative stream cleanup failed", exc_info=True)
            raise
        await pump_task
        message = self._assemble_streamed_message(
            "".join(content_parts), tool_calls_acc)
        from types import SimpleNamespace
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
            usage=usage,
        )

    @staticmethod
    def _assemble_streamed_message(content: str, tool_calls_acc: Dict[int, Dict[str, Any]]):
        """Reconstruct a non-streamed-equivalent completion message from
        accumulated deltas.

        Prefers the real OpenAI model types so a tool-round message can be
        appended back into ``messages`` and serialized on the next API call;
        falls back to attribute-compatible namespaces when unavailable.
        """
        from types import SimpleNamespace
        tool_calls = None
        if tool_calls_acc:
            ordered = [acc for _idx, acc in sorted(tool_calls_acc.items())]
            try:
                from openai.types.chat.chat_completion_message_tool_call import (
                    ChatCompletionMessageToolCall, Function)
                tool_calls = [
                    ChatCompletionMessageToolCall(
                        id=acc["id"] or f"call_{i}", type="function",
                        function=Function(name=acc["name"], arguments=acc["arguments"]))
                    for i, acc in enumerate(ordered)
                ]
            except Exception:
                tool_calls = [
                    SimpleNamespace(
                        id=acc["id"] or f"call_{i}", type="function",
                        function=SimpleNamespace(name=acc["name"],
                                                 arguments=acc["arguments"]))
                    for i, acc in enumerate(ordered)
                ]
        try:
            from openai.types.chat import ChatCompletionMessage
            return ChatCompletionMessage(
                role="assistant", content=content or None, tool_calls=tool_calls)
        except Exception:
            return SimpleNamespace(role="assistant", content=content,
                                   tool_calls=tool_calls)

    async def _emit_narrative_frame(self, websocket, chat_id, stream_id, seq,
                                    text, *, terminal):
        """Send one narrative ``ui_stream_data`` frame (existing wire shape).

        Dual shape per 026 FR-018: structured component for native clients
        plus a web HTML fragment when renderable. The terminal frame carries
        empty content so every client clears the stream node — the turn's
        final ``ui_render`` is the authoritative replacement.
        """
        if text:
            component = Text(content=text, variant="markdown").to_dict()
        else:
            component = {"type": "text", "content": ""}
        components = [component]
        html = "" if terminal else None
        if not terminal:
            try:
                profile = self.rote.get_profile(websocket)
                adapted = self.rote.adapt(websocket, [component])
                if adapted:
                    components = adapted
                from webrender import render_component_fragment
                html = render_component_fragment(
                    components[0] if components else component, profile)
            except Exception:
                logger.debug("narrative frame render failed (structured only)",
                             exc_info=True)
        await self._safe_send(websocket, json.dumps({
            "type": "ui_stream_data",
            "stream_id": stream_id,
            "session_id": chat_id,
            "seq": seq,
            "components": components,
            "html": html,
            "raw": None,
            "terminal": terminal,
            "error": None,
        }))

    _REASONING_EFFORTS = ("minimal", "low", "medium", "high")

    @classmethod
    def _valid_reasoning_effort(cls, value):
        """Normalize a reasoning-effort value. Returns the lowercased value if
        it is one of the recognized levels, else None (so an unset or garbage
        env/arg is simply not sent)."""
        if value is None:
            return None
        v = str(value).strip().lower()
        return v if v in cls._REASONING_EFFORTS else None

    @staticmethod
    def _llm_unsupported_extras(error_str: str, extra_kwargs: Dict[str, Any]) -> set:
        """Capability probe: given a failed completion's error text and the
        optional enhancement params we sent, return the subset the endpoint
        appears to reject (so the caller can drop + remember them).

        Conservative: only fires on signals that look like an unsupported /
        malformed *parameter* (not a transient 5xx or an auth error). When the
        message names a specific param, only that one is dropped; when it is a
        generic "unsupported parameter" 400 that names none of ours, all active
        enhancement params are dropped (they are optional, so dropping them to
        keep the call working is always safe).
        """
        if not extra_kwargs:
            return set()
        low = (error_str or "").lower()
        # Never treat transient/auth failures as a param-capability problem.
        if any(code in low for code in ("502", "503", "504", "bad gateway",
                                        "service unavailable", "connection",
                                        "timeout", "401", "403")):
            return set()
        named = {p for p in extra_kwargs if p in low}
        if named:
            return named
        generic = any(sig in low for sig in (
            "unsupported", "unrecognized", "unexpected keyword", "unknown parameter",
            "unknown field", "not supported", "invalid parameter", "extra inputs",
            "no longer supported", "is not permitted", "unknown argument",
        ))
        if generic and ("400" in low or "param" in low or "argument" in low
                        or "field" in low or "input" in low):
            return set(extra_kwargs)
        return set()

    async def _call_llm_json(self, websocket, messages, *, schema=None,
                             schema_name: str = "result", temperature=None,
                             feature: str = "structured", reasoning_effort=None):
        """Request enforced structured (JSON) output and parse it.

        Passes a ``response_format`` (a strict ``json_schema`` block when
        ``schema`` is given, else plain ``json_object``) through
        :meth:`_call_llm`, which probe-and-falls-back if the endpoint can't do
        it. Returns the parsed object, or ``None`` when the call failed or the
        content was not valid JSON — callers keep their existing best-effort
        JSON-repair path as the fallback, so this is always safe to adopt.
        """
        if schema is not None:
            response_format = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema, "strict": True},
            }
        else:
            response_format = {"type": "json_object"}
        msg, _usage = await self._call_llm(
            websocket, messages, tools_desc=None, temperature=temperature,
            feature=feature, response_format=response_format,
            reasoning_effort=reasoning_effort,
        )
        content = getattr(msg, "content", None) if msg is not None else None
        if not content:
            return None
        try:
            return json.loads(content)
        except (ValueError, TypeError):
            # Tolerate a fenced ```json block or surrounding prose.
            extracted = self._extract_json_block(content)
            if extracted is not None:
                try:
                    return json.loads(extracted)
                except (ValueError, TypeError):
                    return None
            return None

    @staticmethod
    def _extract_json_block(text: str):
        """Best-effort: pull the first balanced JSON object/array out of a
        string that may be wrapped in a ```json fence or prose. Returns the
        substring or None."""
        if not isinstance(text, str):
            return None
        s = text.strip()
        if s.startswith("```"):
            # strip a leading fence line and a trailing fence
            nl = s.find("\n")
            if nl != -1:
                s = s[nl + 1:]
            if s.rstrip().endswith("```"):
                s = s.rstrip()[:-3]
            s = s.strip()
        starts = [i for i in (s.find("{"), s.find("[")) if i != -1]
        if not starts:
            return None
        start = min(starts)
        open_ch = s[start]
        close_ch = "}" if open_ch == "{" else "]"
        depth = 0
        for i in range(start, len(s)):
            if s[i] == open_ch:
                depth += 1
            elif s[i] == close_ch:
                depth -= 1
                if depth == 0:
                    return s[start:i + 1]
        return None

    @staticmethod
    def _tool_result_to_llm_content(res) -> str:
        """Two-tier tool output: the text a tool result contributes to the LLM
        conversation.

        A tool may split its result into a short model-facing tier and a larger
        renderer-only tier. Precedence:

        1. ``_model_digest`` — the explicit model-facing digest. When present it
           is the ONLY thing the LLM sees; the render-only payload
           (``_ui_components`` / ``_data`` / raw fetched text) never enters the
           model. This both cuts tokens and closes a prompt-injection channel
           (untrusted fetched/parsed content stops reaching the reasoning loop).
        2. ``_data`` — the existing convention; serialized as today.
        3. otherwise the whole result is serialized — unchanged behavior.

        Defaulting to (2)/(3) keeps every current tool byte-identical; the
        digest tier is purely opt-in for a tool that sets ``_model_digest``.
        """
        if res is None:
            return "No output"
        if getattr(res, "error", None):
            return f"Error: {res.error.get('message')}"
        result = getattr(res, "result", None)
        if not result:
            return "No output"
        if isinstance(result, dict) and result.get("_model_digest") is not None:
            digest = result["_model_digest"]
            return digest if isinstance(digest, str) else json.dumps(digest)
        if isinstance(result, dict) and "_data" in result:
            return json.dumps(result["_data"])
        return json.dumps(result)

    @staticmethod
    def _result_has_model_digest(res) -> bool:
        """True when a tool result carries a ``_model_digest`` — i.e. the
        LLM-visible text is tool-authored (trusted) and should NOT be wrapped
        as untrusted by datamarking."""
        result = getattr(res, "result", None)
        return isinstance(result, dict) and result.get("_model_digest") is not None

    async def _emit_llm_usage_report(self, websocket, *, feature, model, usage, outcome):
        """Send an ``llm_usage_report`` message to ``websocket`` carrying the
        token-usage tally for one LLM-dependent call (feature 006 FR-014).

        Only invoked when the call's credential source was ``user`` —
        operator-default calls are NOT reported to the per-device
        token-usage counters (FR-016). Best-effort fire-and-forget;
        failures here never affect the LLM call's user-facing result.
        """
        try:
            from datetime import datetime, timezone
            payload = {
                "type": "llm_usage_report",
                "feature": feature,
                "model": model,
                "total_tokens": getattr(usage, "total_tokens", None) if usage else None,
                "prompt_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
                "completion_tokens": getattr(usage, "completion_tokens", None) if usage else None,
                "outcome": outcome,
                "at": datetime.now(timezone.utc).isoformat(),
            }
            await self._safe_send(websocket, json.dumps(payload))
        except Exception as exc:  # pragma: no cover — best-effort delivery
            logger.debug(f"llm_usage_report send failed (non-fatal): {exc}")

    async def _generate_tool_summary(self, websocket, messages, chat_id=None, user_id=None):
        """
        Generate an LLM summary/analysis of accumulated tool results.
        Called when the Re-Act loop ends (max turns or completion) to ensure
        the user always gets a meaningful summary rather than a 'stopped' message.

        Feature 006: routes through the per-user / operator-default
        credential resolver. ``LLMUnavailable`` (no credentials available)
        emits an llm_unconfigured audit event and returns None silently —
        a missing summary is non-fatal and the user already has the
        primary tool output.
        """
        feature = "tool_summary"
        actor_user_id, auth_principal = self._llm_audit_principals(websocket)
        try:
            client, source, resolved = await self._resolve_llm_client_for(websocket)
        except self._LLMUnavailable:
            await self._record_llm_unconfigured(
                self.audit_recorder,
                actor_user_id=actor_user_id,
                auth_principal=auth_principal,
                feature=feature,
            )
            return None

        try:
            # Build a summary-focused prompt from the conversation so far
            summary_messages = [
                {
                    "role": "system",
                    "content": (
                        "You are summarizing the results of tool operations that were just performed. "
                        "Provide a concise, insightful analysis of what was accomplished and the key findings. "
                        "Focus on actionable insights, important numbers, and recommendations. "
                        "Do NOT mention internal details like tool names, turn counts, or system mechanics. "
                        "Write as if you are presenting results to the user directly. "
                        "Keep it to 2-4 sentences."
                    ),
                },
            ]

            # Include relevant parts of the conversation (last several messages)
            for msg in messages[-8:]:
                if isinstance(msg, dict):
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if role in ("user", "tool", "assistant") and content:
                        # Truncate long tool outputs
                        if len(str(content)) > 1500:
                            content = str(content)[:1500] + "..."
                        summary_messages.append({"role": role if role != "tool" else "user", "content": str(content)})

            summary_messages.append({
                "role": "user",
                "content": "Based on the tool results above, provide a brief summary and analysis."
            })

            response = await asyncio.to_thread(
                client.chat.completions.create,
                model=resolved.model,
                messages=summary_messages,
                max_tokens=300,
            )
            usage = getattr(response, "usage", None)
            self._accumulate_usage(chat_id, usage)
            total_tokens = getattr(usage, "total_tokens", None) if usage else None
            await self._record_llm_call(
                self.audit_recorder,
                actor_user_id=actor_user_id,
                auth_principal=auth_principal,
                feature=feature,
                credential_source=source,
                resolved=resolved,
                total_tokens=total_tokens,
                outcome="success",
            )
            if source == self._CredentialSource.USER and websocket is not None:
                await self._emit_llm_usage_report(
                    websocket, feature=feature, model=resolved.model,
                    usage=usage, outcome="success",
                )

            raw_summary = strip_reasoning_markup(response.choices[0].message.content or "").strip()
            summary_text = _strip_toolcall_leakage(raw_summary)
            if raw_summary and not summary_text:
                _log_stripped_empty("tool_summary", chat_id, raw_summary)
                summary_text = _LEAK_FALLBACK_TEXT

            if summary_text:
                # Feature 029 (FR-027): contextual title over the constant
                # "Summary" — derived from the summary's own first heading.
                return [
                    Card(title=self._derive_chat_title(summary_text, default="Round results"),
                         content=[
                             Text(content=summary_text, variant="body")
                         ]).to_dict()
                ]

        except Exception as e:
            error = self._safe_llm_error_metadata(e)
            logger.warning(
                "Tool-summary LLM call failed exception_class=%s "
                "status_code=%s upstream_error_class=%s",
                error.exception_class,
                error.status_code,
                error.upstream_error_class,
            )
            await self._record_llm_call(
                self.audit_recorder,
                actor_user_id=actor_user_id,
                auth_principal=auth_principal,
                feature=feature,
                credential_source=source,
                resolved=resolved,
                total_tokens=None,
                outcome="failure",
                upstream_error_class=error.upstream_error_class,
            )
            if source == self._CredentialSource.USER and websocket is not None:
                await self._emit_llm_usage_report(
                    websocket, feature=feature, model=resolved.model,
                    usage=None, outcome="failure",
                )

        return None

    # =========================================================================
    # CONSTANTS
    # =========================================================================

    MAX_RETRIES = 3
    RETRY_BACKOFF = [1.0, 2.0, 4.0]  # exponential backoff

    def _find_tool_owner(self, tool_name: str) -> Optional[str]:
        """Return the agent_id that owns ``tool_name`` (any registered agent), or None.

        Searches every entry in ``self.agent_cards`` regardless of filter state —
        the goal is to identify the owner so we can name it in the disabled-tool
        alert, not to gate dispatch.
        """
        if not tool_name:
            return None
        for agent_id, card in self.agent_cards.items():
            for skill in getattr(card, "skills", []) or []:
                if getattr(skill, "id", None) == tool_name:
                    return agent_id
        return None

    def _diagnose_disabled_tool(
        self,
        tool_name: str,
        user_id: Optional[str],
        chat_id: Optional[str],
    ) -> ToolDiagnostic:
        """Determine why ``tool_name`` may be unavailable for ``user_id`` in ``chat_id``.

        Mirrors the filter stack in handle_chat_message's tool-list build
        (orchestrator.py around lines 2080–2140). Priority order — first match wins:
        UNKNOWN_TOOL > AGENT_DISABLED_BY_USER > SECURITY_BLOCKED > PERMISSION_DENIED >
        DISABLED_IN_PICKER > ENABLED.
        """
        agent_id = self._find_tool_owner(tool_name)
        if not agent_id:
            return ToolDiagnostic(
                status=ToolDiagnosticStatus.UNKNOWN_TOOL,
                agent_id=None,
                agent_display_name=None,
                reason=None,
            )
        card = self.agent_cards.get(agent_id)
        agent_display = (
            getattr(card, "name", None) or agent_id if card is not None else agent_id
        )

        # 1) User has disabled the whole agent.
        if user_id:
            try:
                disabled = set(self.history.db.get_user_disabled_agents(user_id))
            except Exception:  # pragma: no cover — defensive
                disabled = set()
            if agent_id in disabled:
                return ToolDiagnostic(
                    status=ToolDiagnosticStatus.AGENT_DISABLED_BY_USER,
                    agent_id=agent_id,
                    agent_display_name=agent_display,
                    reason=None,
                )

        # 2) System-blocked (proactive security review).
        flags = getattr(self, "security_flags", {}).get(agent_id, {}) or {}
        flag = flags.get(tool_name) or {}
        if flag.get("blocked"):
            return ToolDiagnostic(
                status=ToolDiagnosticStatus.SECURITY_BLOCKED,
                agent_id=agent_id,
                agent_display_name=agent_display,
                reason=flag.get("reason"),
            )

        # 3) Permission / scope denial.
        if user_id:
            try:
                allowed = self.tool_permissions.is_tool_allowed(user_id, agent_id, tool_name)
            except Exception:  # pragma: no cover — defensive
                # Fail closed: a permission-check error must not report the tool
                # as effectively allowed in the disabled-tool diagnostic.
                allowed = False
            if not allowed:
                return ToolDiagnostic(
                    status=ToolDiagnosticStatus.PERMISSION_DENIED,
                    agent_id=agent_id,
                    agent_display_name=agent_display,
                    reason=None,
                )

        # 4) Per-chat tool picker.
        if user_id and chat_id:
            try:
                bound_agent_id = self.history.db.get_chat_agent(chat_id)
            except Exception:  # pragma: no cover — defensive
                bound_agent_id = None
            if bound_agent_id is not None:
                try:
                    saved = self.history.db.get_user_tool_selection(user_id, bound_agent_id)
                except Exception:  # pragma: no cover — defensive
                    saved = None
                if saved is not None and len(saved) > 0 and tool_name not in saved:
                    return ToolDiagnostic(
                        status=ToolDiagnosticStatus.DISABLED_IN_PICKER,
                        agent_id=agent_id,
                        agent_display_name=agent_display,
                        reason=None,
                    )

        return ToolDiagnostic(
            status=ToolDiagnosticStatus.ENABLED,
            agent_id=agent_id,
            agent_display_name=agent_display,
            reason=None,
        )

    @staticmethod
    def _alert_for_disabled_tool(diag: ToolDiagnostic, tool_name: str) -> Alert:
        """Render a user-facing Alert explaining why ``tool_name`` was unavailable.

        Variants follow existing conventions: 'warning' for things the user
        can self-correct (picker, agent toggle, permission), 'error' for
        admin-blocked or unknown-tool states, 'info' for the surprising
        ENABLED-but-format-mismatch case (only reachable from the leak path).
        """
        agent_label = diag.agent_display_name or diag.agent_id or "an installed agent"
        if diag.status is ToolDiagnosticStatus.DISABLED_IN_PICKER:
            return Alert(
                message=(
                    f"The assistant tried to use the **{tool_name}** tool "
                    f"(from the **{agent_label}** agent), but that tool is "
                    f"turned off in your tool picker for this chat. "
                    f"Re-enable it in the picker to use it."
                ),
                variant="warning",
                title="Tool disabled",
            )
        if diag.status is ToolDiagnosticStatus.AGENT_DISABLED_BY_USER:
            return Alert(
                message=(
                    f"The assistant tried to use the **{tool_name}** tool, "
                    f"but the **{agent_label}** agent is disabled. "
                    f"Open Agents settings and turn it on to use this tool."
                ),
                variant="warning",
                title="Agent disabled",
            )
        if diag.status is ToolDiagnosticStatus.PERMISSION_DENIED:
            return Alert(
                message=(
                    f"The assistant tried to use the **{tool_name}** tool "
                    f"(from the **{agent_label}** agent), but it is restricted "
                    f"by permissions. Open the agent's permissions panel and "
                    f"grant the right scope."
                ),
                variant="warning",
                title="Tool restricted",
            )
        if diag.status is ToolDiagnosticStatus.SECURITY_BLOCKED:
            reason = diag.reason or "system policy"
            return Alert(
                message=(
                    f"The assistant tried to use the **{tool_name}** tool, "
                    f"but it is system-blocked: {reason}. An administrator "
                    f"must unblock it before it can be used."
                ),
                variant="error",
                title="Tool blocked",
            )
        if diag.status is ToolDiagnosticStatus.UNKNOWN_TOOL:
            return Alert(
                message=(
                    f"The assistant tried to use a tool named **{tool_name}**, "
                    f"but no installed agent provides it. The model may have "
                    f"hallucinated the name — try rephrasing your request."
                ),
                variant="error",
                title="Unknown tool",
            )
        # ENABLED — only reachable from the leak path (not from the dispatch
        # gate, which short-circuits before this is rendered).
        return Alert(
            message=(
                f"The assistant emitted tool-call markup that this orchestrator "
                f"doesn't recognize. The **{tool_name}** tool exists and is "
                f"enabled — try switching to a model that uses native tool "
                f"calling, or configure your LLM endpoint to emit OpenAI-format "
                f"tool calls."
            ),
            variant="info",
            title="Unrecognized tool-call format",
        )

    def _diagnose_leaked_tool_calls(
        self,
        content: str,
        user_id: Optional[str],
        chat_id: Optional[str],
    ) -> List[Dict[str, Any]]:
        """Inspect ``content`` for leaked tool-call markup and return one Alert
        (as a serialized component dict) per distinct tool name found.

        Only returns alerts for tool names actually parsed out of the markup —
        if the leak regex matches but no recognizable tool name can be
        extracted, returns an empty list (the existing strip behavior remains
        the only mitigation for that case).
        """
        if not content:
            return []
        # Quick pre-filter: only walk extractors when at least one leak pattern fired.
        if not any(p.search(content) for p in _LEAKED_TOOL_CALL_PATTERNS):
            return []
        names = _tool_names_from_leak(content)
        if not names:
            return []
        alerts: List[Dict[str, Any]] = []
        for tool_name in names:
            diag = self._diagnose_disabled_tool(tool_name, user_id, chat_id)
            alert = self._alert_for_disabled_tool(diag, tool_name)
            alerts.append(alert.to_dict())
        return alerts

    def _is_long_running_tool(self, agent_id: Optional[str], tool_name: str) -> bool:
        """Return True if the agent's card declares this tool as long-running (FR-026)."""
        if not agent_id:
            return False
        card = self.agent_cards.get(agent_id)
        if not card:
            return False
        md = getattr(card, "metadata", {}) or {}
        return tool_name in (md.get("long_running_tools") or [])

    def _policy_roles(self, websocket) -> List[str]:
        """Best-effort session roles for the policy engine. Handles a flat
        ``roles`` claim or the Keycloak ``realm_access.roles`` shape; returns
        ``[]`` when unavailable (role-predicated rules simply won't match)."""
        claims = self.ui_sessions.get(websocket) if websocket is not None else None
        if not isinstance(claims, dict):
            return []
        roles = claims.get("roles")
        if not roles:
            ra = claims.get("realm_access")
            roles = ra.get("roles") if isinstance(ra, dict) else None
        return list(roles) if isinstance(roles, (list, tuple)) else []

    def _taint_tracker(self, chat_id: Optional[str]):
        """Per-chat taint tracker (the data-flow scope), lazily created."""
        store = getattr(self, "_taint_trackers", None)
        if store is None:
            store = {}
            self._taint_trackers = store
        key = chat_id or "_global"
        tracker = store.get(key)
        if tracker is None:
            from orchestrator.taint import TaintTracker
            tracker = TaintTracker()
            store[key] = tracker
        return tracker

    def _skill_store(self, user_id):
        """Per-user in-memory learned-recipe store (skill memory, C-N10)."""
        if not hasattr(self, "_skill_recipes"):
            self._skill_recipes = {}
        return self._skill_recipes.setdefault(user_id or "_anon", [])

    def mint_action_token(self, agent_id, user_id, tool_name, args):
        """Mint a single-use authorization token (C-S8) for a confirmed high-risk
        call, bound to (agent, user, tool, hash(args)). This is the issue half of
        the require_token policy effect — a confirmed call carries the token as
        ``_txn_token`` and passes the gate's verify-and-consume. Returns None when
        no signing key (TXN_TOKEN_KEY / MEMORY_HMAC_KEY) is configured."""
        try:
            from orchestrator import transaction_token as _txn
            return _txn.mint(agent_id or "", user_id or "", tool_name, args)
        except Exception:
            logger.debug("mint_action_token failed", exc_info=True)
            return None

    async def _authorize_and_prepare(
        self, websocket, agent_id: Optional[str], tool_name: str, args: Dict,
        chat_id: str = None, user_id: str = None, *,
        stream_params: Optional[Dict] = None,
        parent_token: Optional[Dict[str, Any]] = None,
        initiating_agent_id: Optional[str] = None,
    ):
        """Run the gate stack, auditing a HOP's refusal (056 SC-002).

        Thin wrapper over :meth:`_run_gate_stack`. A chained hop refused by any
        gate — including the ones that refuse before the delegation step
        (security flag, permission/opt-out, policy, taint, supervisor, HITL,
        cap) — emits a ``delegation.hop.mint`` failure record, so 100% of
        gate-violating hop attempts carry audit evidence. Direct dispatch is
        unchanged (no hop record, exactly as today).
        """
        outcome = await self._run_gate_stack(
            websocket, agent_id, tool_name, args, chat_id, user_id,
            stream_params=stream_params, parent_token=parent_token,
            initiating_agent_id=initiating_agent_id)
        if parent_token is not None and isinstance(outcome, GateRefusal) \
                and not outcome.hop_audited:
            from audit.recorder import make_correlation_id
            await self._record_hop_audit(
                operation="mint", outcome="failure", parent=parent_token,
                child=None, callee_agent_id=agent_id or "", tool_name=tool_name,
                chat_id=chat_id, correlation_id=make_correlation_id(),
                detail=(outcome.response.error or {}).get("message", "")[:200])
        return outcome

    async def _run_gate_stack(
        self, websocket, agent_id: Optional[str], tool_name: str, args: Dict,
        chat_id: str = None, user_id: str = None, *,
        stream_params: Optional[Dict] = None,
        parent_token: Optional[Dict[str, Any]] = None,
        initiating_agent_id: Optional[str] = None,
    ):
        """Run the FULL single-path gate stack once (056 US3, FR-017/SC-006).

        Shared by ``execute_single_tool``, ``execute_parallel_tools``, and
        every chained hop (which re-enters ``execute_single_tool``), so a
        violating call is refused identically on every dispatch path. Applies,
        in the single path's historical order: the system security-flag block,
        the per-user tool permission gate, the deterministic policy engine,
        the taint/data-flow sink gate, the intent-alignment supervisor + HITL,
        file-path mapping, per-(user, callee) credential injection, LLM
        credential surfacing (054), the disabled-tool diagnosis gate, the
        no-agent check, the RFC 8693 delegation-token mint (fail-closed in
        production posture), the PRE_TOOL_USE hook, the concurrency cap, and
        the 055 stream auto-subscribe.

        Returns :class:`PreparedDispatch` on allow or :class:`GateRefusal` on
        any deny — gate logic only, no UI delivery: each caller renders the
        refusal per its own delivery model (immediate for the single path,
        batched for the parallel path), keeping wire behavior byte-identical.

        ``parent_token`` (056 US1, wired by the chained-hop seam) switches the
        delegation step from the flat single-hop exchange to a child mint;
        ``initiating_agent_id`` additionally charges the initiating agent's
        concurrency slot on long-running hops (FR-019).
        """
        if stream_params is None:
            stream_params = dict(args)

        # System-level security block (proactive security review)
        agent_flags = self.security_flags.get(agent_id, {}) if agent_id else {}
        if agent_id and tool_name in agent_flags and agent_flags[tool_name].get("blocked"):
            reason = agent_flags[tool_name].get("reason", "Security threat detected")
            err_msg = f"Tool '{tool_name}' is system-blocked: {reason}"
            logger.warning(f"Security block: agent={agent_id} tool={tool_name}")
            alert = Alert(message=err_msg, variant="error")
            return GateRefusal(
                response=MCPResponse(
                    error={"message": err_msg, "retryable": False}),
                render_components=[alert.to_dict()],
                render_target="chat")

        # Permission enforcement gate (RFC 8693 delegation)
        if user_id and agent_id and not await asyncio.to_thread(
                self.tool_permissions.is_tool_allowed, user_id, agent_id, tool_name):
            err_msg = f"Tool '{tool_name}' is restricted for this agent. Update permissions in the sidebar to enable it."
            logger.warning(f"Permission denied: user={user_id} agent={agent_id} tool={tool_name}")
            alert = Alert(message=err_msg, variant="warning")
            return GateRefusal(
                response=MCPResponse(
                    error={"message": err_msg, "retryable": False}),
                render_components=[alert.to_dict()])

        # Feature 063 US3: destructive-operation confirmation gate. Every dispatch
        # path (single, parallel, chained hop, component re-exec) reaches here via
        # _run_gate_stack, so this one check cannot be bypassed. It runs BEFORE args
        # are mutated (clean sha256 fingerprint) and BEFORE credentials/delegation
        # tokens are minted for a call that may be refused. remote-compute-1 only;
        # evaluate() fires only for that agent's DESTRUCTIVE verbs (read verbs and
        # non-destructive mutating verbs classify to None and pass straight through).
        _session_claims = self.ui_sessions.get(websocket, {}) if websocket is not None else {}
        if (
            agent_id == "remote-compute-1"
            and _session_claims.get("_invocation_channel") == "mcp"
        ):
            from orchestrator import remote_confirmation
            if remote_confirmation.is_destructive_unattended(tool_name, args):
                err_msg = (
                    f"Tool '{tool_name}' is destructive and cannot run over the "
                    "unattended MCP channel; use an interactive Astral session."
                )
                logger.warning(
                    "mcp destructive refusal agent=%s tool=%s",
                    agent_id,
                    tool_name,
                )
                return GateRefusal(
                    response=MCPResponse(
                        error={"message": err_msg, "retryable": False}
                    ),
                    render_components=[
                        Alert(message=err_msg, variant="error").to_dict()
                    ],
                    render_target="chat",
                )

        if agent_id == "remote-compute-1":
            from orchestrator import remote_confirmation
            _conf = await asyncio.to_thread(
                remote_confirmation.evaluate, self, websocket, agent_id, tool_name,
                args, chat_id, user_id)
            if _conf is not None:
                _msg, _comps = _conf
                return GateRefusal(
                    response=MCPResponse(
                        error={"message": _msg, "retryable": False}),
                    render_components=_comps, render_target="chat")

        # Deterministic pre-action policy engine — an ordered, fail-closed rule
        # chain (data, admin-extensible via POLICY_RULES) on top of the
        # permission gate. Default OFF + no seed rules ⇒ purely additive.
        # deny/confirm block the call; rewrite redacts args before execution.
        if user_id:
            from orchestrator import policy
            if policy.policy_enabled():
                try:
                    decision = policy.evaluate_policy(
                        policy.load_rules(),
                        {"tool": tool_name, "agent": agent_id, "user_id": user_id,
                         "roles": self._policy_roles(websocket), "args": args})
                except Exception:
                    logger.debug("policy: evaluation failed — allowing", exc_info=True)
                    decision = policy.PolicyDecision()
                # A require_token rule demands a valid single-use transaction
                # token bound to (agent, user, tool, hash(args)). Fail-closed —
                # missing/tampered/expired/replayed ⇒ deny.
                if decision.effect == policy.REQUIRE_TOKEN:
                    from orchestrator import transaction_token as _txn
                    token = args.get("_txn_token") if isinstance(args, dict) else None
                    ok_tok, why = _txn.verify_and_consume(
                        _txn.default_store(), token, agent_id or "", user_id,
                        tool_name, args)
                    if not ok_tok:
                        msg = decision.reason or (
                            f"'{tool_name}' needs a valid one-time authorization "
                            f"token ({why}).")
                        logger.warning("policy.require_token user=%s tool=%s rule=%s reason=%s",
                                       user_id, tool_name, decision.rule_id, why)
                        alert = Alert(message=msg, variant="warning")
                        return GateRefusal(
                            response=MCPResponse(
                                error={"message": msg, "retryable": False}),
                            render_components=[alert.to_dict()])
                elif decision.effect in (policy.DENY, policy.CONFIRM):
                    msg = decision.reason or (
                        f"'{tool_name}' needs confirmation before it can run."
                        if decision.effect == policy.CONFIRM
                        else f"'{tool_name}' was blocked by an access policy.")
                    logger.warning("policy.%s user=%s tool=%s rule=%s",
                                   decision.effect, user_id, tool_name, decision.rule_id)
                    alert = Alert(message=msg, variant="warning")
                    return GateRefusal(
                        response=MCPResponse(
                            error={"message": msg, "retryable": False}),
                        render_components=[alert.to_dict()])
                if decision.args is not None:
                    args = decision.args  # rewritten (e.g. a secret arg redacted)
                    # The streaming twin must dispatch the SAME redacted args —
                    # auto-subscribe with the pre-rewrite capture would hand
                    # the agent the secret the rule just removed.
                    stream_params = dict(args)
                # Never forward a consumed authorization token to the agent.
                if isinstance(args, dict) and "_txn_token" in args:
                    args = {k: v for k, v in args.items() if k != "_txn_token"}

        # Value-level taint/data-flow gate. If this call is a write/egress SINK
        # and its arguments carry untrusted-tainted values (effective trust =
        # min over data ancestors, recorded from prior untrusted-source outputs
        # — survives multi-hop laundering), refuse it. Flag-gated (default OFF)
        # + fail-open: unknown values are trusted, so a call with only
        # constants/user intent always passes.
        if user_id:
            from orchestrator import taint as _taint
            if _taint.taint_enabled() and _taint.is_sink(agent_id, tool_name):
                tracker = self._taint_tracker(chat_id)
                trust = tracker.effective_trust_of_args(args)
                if _taint.check_flow(trust) == "deny":
                    msg = (f"'{tool_name}' was blocked: it would send untrusted "
                           f"data (from a web/third-party source) into a "
                           f"write/egress action.")
                    logger.warning("taint.deny user=%s tool=%s agent=%s trust=%s",
                                   user_id, tool_name, agent_id, _taint.trust_name(trust))
                    alert = Alert(message=msg, variant="warning")
                    return GateRefusal(
                        response=MCPResponse(
                            error={"message": msg, "retryable": False}),
                        render_components=[alert.to_dict()])

        # Intent-alignment supervisor (C-S5) + high-risk human-in-the-loop
        # (C-S11). Both default OFF and fail-open: when on, a destructive tool
        # the user never asked for, or a risky (egress/irreversible/cross-
        # principal/tainted) call, is held for confirmation instead of running.
        if user_id and agent_id:
            from orchestrator import supervisor as _sup
            if _sup.supervisor_enabled():
                request_text = _ACTIVE_REQUEST_TEXT.get() or getattr(
                    self, "_active_request", {}
                ).get(chat_id, "")
                if not _sup.intent_aligned(request_text, tool_name):
                    msg = (f"'{tool_name}' looks like a destructive action you "
                           f"didn't ask for — please confirm before it runs.")
                    logger.warning("supervisor.escalate user=%s tool=%s", user_id, tool_name)
                    alert = Alert(message=msg, variant="warning")
                    return GateRefusal(
                        response=MCPResponse(
                            error={"message": msg, "retryable": False}),
                        render_components=[alert.to_dict()])
            from orchestrator import hitl as _hitl
            if _hitl.hitl_enabled():
                trust = "trusted"
                try:
                    from orchestrator import taint as _tnt
                    if _tnt.taint_enabled():
                        trust = _tnt.trust_name(
                            self._taint_tracker(chat_id).effective_trust_of_args(args))
                except Exception:
                    trust = "trusted"
                risks = _hitl.assess_risk(tool_name, args, actor_principal=user_id, trust=trust)
                if _hitl.requires_confirmation(risks):
                    req = _hitl.confirmation_request(tool_name, risks)
                    logger.warning("hitl.confirm user=%s tool=%s risks=%s", user_id, tool_name, risks)
                    alert = Alert(message=req.summary, variant="warning")
                    return GateRefusal(
                        response=MCPResponse(
                            error={"message": req.summary, "retryable": False}),
                        render_components=[alert.to_dict()])

        # Map file paths if chat_id provided
        if chat_id:
            args = await asyncio.to_thread(
                self._map_file_paths, chat_id, args, user_id=user_id)
            args["session_id"] = chat_id
            if user_id:
                args["user_id"] = user_id

        # Inject per-user credentials (E2E encrypted — only agent can decrypt)
        if user_id and agent_id:
            creds = await asyncio.to_thread(
                self.credential_manager.get_agent_credentials_encrypted, user_id, agent_id)
            if creds:
                args["_credentials"] = creds
                args["_credentials_encrypted"] = True

        # Feature 054: surface the resolved LLM credentials for THIS call's
        # context so agent-side LLM tools can run (they no longer have any
        # env fallback): the caller's persisted record on a user socket, the
        # admin system record on system-context turns. The kwarg name
        # ``_session_llm_credentials`` is kept for agent-side compatibility.
        #
        # ONLY inject for in-process built-in agents, where the plaintext key
        # stays inside the orchestrator process (and is scrubbed from the
        # deep-copied args in ``_execute_in_process``). A WebSocket/A2A agent —
        # notably a user-created draft or any external agent — would otherwise
        # receive the user's LIVE provider API key in plaintext over the wire
        # on EVERY dispatch, even a tool that never touches an LLM (e.g.
        # ``dice_roller.roll``): the exact inverse of the ECIES per-user-secret
        # boundary. Every bundled LLM-using tool (general, web_research,
        # summarizer) runs in-process, so no legitimate consumer is affected.
        if agent_id in self.local_agents:
            _llm_ctx_user = self._llm_context_user_id(websocket)
            if _llm_ctx_user is None:
                _llm_cfg = await self._llm_store.get_system()
            else:
                _llm_cfg = await self._llm_store.get(_llm_ctx_user)
            if _llm_cfg is not None:
                args["_session_llm_credentials"] = {
                    "OPENAI_API_KEY": _llm_cfg.api_key,
                    "OPENAI_BASE_URL": _llm_cfg.base_url,
                    "LLM_MODEL": _llm_cfg.model,
                }

        # 5th gate (015): when no `agent_id` was resolved via `tool_to_agent` —
        # which happens because the tool was filtered out at chat-time tool-list
        # construction — see whether the tool actually EXISTS on a registered
        # agent and surface a friendly disabled-tool alert. This catches the
        # case where the model emitted a call for a tool the user disabled in
        # the picker (or whose owning agent they disabled wholesale).
        if not agent_id and tool_name:
            owner = self._find_tool_owner(tool_name)
            if owner is not None:
                diag = await asyncio.to_thread(
                    self._diagnose_disabled_tool, tool_name, user_id, chat_id)
                alert = self._alert_for_disabled_tool(diag, tool_name)
                logger.info(
                    "Dispatch blocked by disabled-tool gate: tool=%s owner=%s status=%s user=%s chat=%s",
                    tool_name, owner, diag.status.value, user_id, chat_id,
                )
                return GateRefusal(
                    response=MCPResponse(
                        error={"message": alert.message, "retryable": False}),
                    render_components=[alert.to_dict()],
                    render_target="chat")

        if not agent_id or (
            agent_id not in self.agents
            and agent_id not in self.a2a_clients
            and agent_id not in self.local_agents
        ):
            err_msg = f"No agent available for tool '{tool_name}'"
            return GateRefusal(
                response=MCPResponse(error={"message": err_msg}),
                render_components=[Alert(message=err_msg, variant="error").to_dict()],
                render_target="chat")

        # RFC 8693 delegation: generate a scoped token excluding system-blocked tools
        # The delegation token constrains what the agent can do even if it's compromised
        delegation_token: Optional[str] = None
        hop_correlation_id: Optional[str] = None
        # 058 (no secrets to untrusted agents): a user-hosted (tunnel) agent is
        # untrusted — never hand it the delegation-token BYTES, and never run the
        # flat delegation-required gate against it. The orchestrator re-authorizes
        # every one of its tool calls at the boundary (is_tool_allowed + the full
        # gate stack), so it never needs to hold a token; withholding it removes a
        # forgeable-authority surface (research D2). Its per-(user,callee) ECIES
        # credentials are still injected (encrypted; only the agent boundary
        # decrypts). Hop provenance still mints/audits below — only the token
        # hand-off to the agent is suppressed.
        from shared.local_transport import TunnelSocket
        _untrusted_tunnel_agent = isinstance(self.agents.get(agent_id), TunnelSocket)
        if user_id and agent_id and parent_token is not None:
            # 056 US1 (FR-001/FR-002): a chained hop NEVER reuses the parent's
            # token and never falls back to the flat exchange — it acts under
            # a freshly minted, strictly-narrower child, or is refused. Real
            # minting runs in every posture (dev included, D17.2).
            minted = await self._mint_child_for_hop(
                parent_token, agent_id, tool_name, user_id, chat_id)
            if isinstance(minted, GateRefusal):
                return minted
            delegation_token, hop_correlation_id = minted
            if not _untrusted_tunnel_agent:
                args["_delegation_token"] = delegation_token
        elif user_id and agent_id and not _untrusted_tunnel_agent:
            delegation_token = await self._get_delegation_token(websocket, agent_id, user_id)
            if delegation_token:
                args["_delegation_token"] = delegation_token
            elif self._delegation_required():
                # Feature 030 / Constitution VII: agents MUST act under RFC
                # 8693 delegated tokens. The walkthrough found the deployed
                # realm missing the tools:* client scopes — every exchange
                # failed invalid_scope and dispatch silently proceeded
                # UNSCOPED. Production posture now fails closed with an
                # actionable message; development keeps the fail-open
                # behavior (warned once per agent) so local stacks without a
                # fully configured realm still work. The message has to name
                # the actual fault: an unavailable exchange is the operator's
                # to fix, while an empty scope set is this user's permissions
                # — sending them to a correctly configured realm is a dead end.
                permissions_fault = await self._delegation_denied_for_permissions(
                    websocket, agent_id, user_id)
                if permissions_fault:
                    err_msg = (
                        f"Tool execution is disabled: you haven't granted '{agent_id}' "
                        "any tool permissions, so it cannot act on your behalf. "
                        "Enable the agent's tools under Settings → Agents & permissions."
                    )
                else:
                    err_msg = (
                        "Tool execution is disabled: delegated authorization "
                        "(RFC 8693 token exchange) is unavailable for agent "
                        f"'{agent_id}'. An operator must register the tools:* "
                        "client scopes on the identity provider (see "
                        "docs/keycloak_agent_delegation_setup.md), or set "
                        "DELEGATION_REQUIRED=false to accept unscoped dispatch."
                    )
                logger.error(
                    "Delegation required but unavailable (%s): agent=%s user=%s — refusing dispatch",
                    "no_enabled_scopes" if permissions_fault else "exchange_unavailable",
                    agent_id, user_id,
                )
                return GateRefusal(
                    response=MCPResponse(error={"message": err_msg, "retryable": False}),
                    render_components=[Alert(message=err_msg, variant="error").to_dict()],
                    render_target="chat")

        # Hook: PRE_TOOL_USE — allows handlers to block or modify tool args
        if flags.is_enabled("hook_system"):
            hook_ctx = HookContext(
                event=HookEvent.PRE_TOOL_USE,
                user_id=user_id or "",
                agent_id=agent_id or "",
                tool_name=tool_name,
                tool_args=args,
            )
            hook_resp = await self.hooks.emit(hook_ctx)
            if hook_resp.action == "block":
                err_msg = f"Tool '{tool_name}' blocked by hook: {hook_resp.reason or 'no reason given'}"
                logger.info(f"Hook blocked tool: {tool_name}")
                return GateRefusal(
                    response=MCPResponse(error={"message": err_msg, "retryable": False}))
            if hook_resp.action == "modify" and hook_resp.modified_args:
                args = hook_resp.modified_args

        # 015-external-ai-agents: concurrency cap for long-running tools (FR-026).
        # Acquired here so a 4th concurrent attempt is rejected without ever
        # touching the upstream service. Released either on dispatch error
        # (below) or by the terminal-phase ToolProgress handler.
        cap_job_id: Optional[str] = None
        if user_id and agent_id and self._is_long_running_tool(agent_id, tool_name):
            cap_job_id = f"cap_{tool_name}_{_uuid.uuid4().hex[:8]}"
            acquired = await self.concurrency_cap.acquire(user_id, agent_id, cap_job_id)
            if not acquired:
                inflight = self.concurrency_cap.inflight_jobs(user_id, agent_id)
                max_n = self.concurrency_cap.max_per_user_agent
                err_msg = (
                    f"You already have {max_n} jobs running on '{agent_id}'. "
                    f"Wait for one to finish or cancel one before starting another. "
                    f"(Running: {', '.join(inflight)})"
                )
                logger.info(
                    "ConcurrencyCap rejected dispatch: user=%s agent=%s tool=%s",
                    user_id, agent_id, tool_name,
                )
                alert = Alert(message=err_msg, variant="warning")
                return GateRefusal(
                    response=MCPResponse(
                        error={"message": err_msg, "retryable": False}),
                    render_components=[alert.to_dict()],
                    render_target="chat")
            # 056 US3 (FR-019): a chained hop charges BOTH the executing
            # agent's slot (above) and the initiating agent's slot, so fan-out
            # cannot multiply a user's effective concurrency past the per-agent
            # cap. Reject-not-queue is preserved; a rejection surfaces to the
            # requester as an honest per-call failure.
            if initiating_agent_id and initiating_agent_id != agent_id:
                hop_acquired = await self.concurrency_cap.acquire(
                    user_id, initiating_agent_id, cap_job_id)
                if not hop_acquired:
                    await self.concurrency_cap.release(user_id, agent_id, cap_job_id)
                    inflight = self.concurrency_cap.inflight_jobs(
                        user_id, initiating_agent_id)
                    max_n = self.concurrency_cap.max_per_user_agent
                    err_msg = (
                        f"You already have {max_n} jobs running on "
                        f"'{initiating_agent_id}'. "
                        f"Wait for one to finish or cancel one before starting another. "
                        f"(Running: {', '.join(inflight)})"
                    )
                    logger.info(
                        "ConcurrencyCap rejected hop dispatch (initiator slot): "
                        "user=%s initiator=%s callee=%s tool=%s",
                        user_id, initiating_agent_id, agent_id, tool_name,
                    )
                    alert = Alert(message=err_msg, variant="warning")
                    return GateRefusal(
                        response=MCPResponse(
                            error={"message": err_msg, "retryable": False}),
                        render_components=[alert.to_dict()],
                        render_target="chat")
                self._hop_cap_entries[cap_job_id] = (user_id, initiating_agent_id)
            args["_cap_job_id"] = cap_job_id
            self._pending_cap_entries[cap_job_id] = (user_id, agent_id)
            # Remember the chat this long-running job belongs to so its progress
            # and final result are delivered to (and persisted in) that chat for
            # any client that returns to it later (014/015 + 028).
            self._job_context[cap_job_id] = {
                "user_id": user_id,
                "agent_id": agent_id,
                "chat_id": chat_id,
                "tool_name": tool_name,
                # A detached terminal result is its own logical update. The
                # UUID is allocated before dispatch and retained across the
                # job lifetime so duplicate terminal delivery cannot invent a
                # second conversation identity.
                "publication_request_generation": str(_uuid.uuid4()),
            }

        # 055 US2: a push-streamable tool also streams — subscribe this
        # user's sockets on the chat before the one-shot dispatch (fail-open).
        await self._auto_subscribe_stream_artifacts(
            websocket, chat_id, user_id, tool_name, stream_params)

        return PreparedDispatch(
            args=args,
            stream_params=stream_params,
            cap_job_id=cap_job_id,
            delegation_token=delegation_token,
            hop_correlation_id=hop_correlation_id,
        )

    async def execute_single_tool(self, websocket, tool_call, tool_to_agent: Dict, chat_id: str = None, user_id: str = None, tool_to_unqualified: Optional[Dict[str, str]] = None, parent_token: Optional[Dict[str, Any]] = None, initiating_agent_id: Optional[str] = None) -> Optional[MCPResponse]:
        """Execute a single tool call and render its UI components. Returns the Result object.

        056 US1: a mediated chained hop re-enters HERE (via
        ``_handle_agent_hop_request``) with ``parent_token`` (the initiator's
        decoded delegation payload, from the orchestrator's own dispatch
        record) and ``initiating_agent_id`` — switching the delegation step to
        a strictly-narrower child mint and charging both sides' concurrency
        slots. Absent both kwargs, behavior is the unchanged direct path."""
        # The LLM may have emitted a qualified name (e.g. "forecaster-1__submit_dataset")
        # when two agents own a tool of the same id. Resolve the bare skill id so the
        # owning agent receives the name it actually registered.
        llm_tool_name = tool_call.function.name
        if tool_to_unqualified and llm_tool_name in tool_to_unqualified:
            tool_name = tool_to_unqualified[llm_tool_name]
        else:
            tool_name = llm_tool_name
        try:
            args = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
        except json.JSONDecodeError as _json_err:
            # Hard-gate malformed tool-call arguments instead of silently
            # dispatching with empty args (silent repair / parser loss).
            # Surface the parse failure back to the model so it can retry
            # with valid JSON — mirrors the permission-denial error return.
            msg = (f"The arguments for '{tool_name}' were not valid JSON "
                   f"({str(_json_err).splitlines()[0]}). Re-emit the tool "
                   f"call with well-formed JSON arguments.")
            logger.warning("tool_arg_parse_fail tool=%s user=%s err=%s",
                           tool_name, user_id, _json_err.msg)
            alert = Alert(message=msg, variant="error")
            await self.send_ui_render(websocket, [alert.to_dict()])
            return MCPResponse(error={"message": msg, "retryable": True})

        # 055 US2: the LLM-authored params as written, captured before
        # path-mapping / credential injection mutate `args` — the stream
        # bridge identity must fingerprint what `_source_params` will carry.
        stream_params = dict(args)

        # Feature 027 — orchestrator meta-tools dispatch before the agent
        # gates (the pseudo-agent has no scopes/credentials; ownership and
        # approval gates live inside the handler — contracts/agentic-creation.md).
        agent_id = tool_to_agent.get(llm_tool_name)
        if agent_id is None and tool_to_agent:
            # 030: weak models routinely mangle hyphen/underscore in
            # collision-qualified names ("web_research-1__web_search" for
            # "web-research-1__web_search"), which used to dead-end as
            # "No agent available" — and then bait the model into creating
            # a duplicate capability. Recover deterministically when the
            # normalized form matches exactly ONE offered tool.
            wanted = llm_tool_name.replace("-", "_").lower()
            matches = [k for k in tool_to_agent
                       if k.replace("-", "_").lower() == wanted]
            if len(matches) == 1:
                logger.info(
                    "Tool name normalized: %r -> %r", llm_tool_name, matches[0])
                llm_tool_name = matches[0]
                tool_name = (tool_to_unqualified or {}).get(llm_tool_name, tool_name)
                agent_id = tool_to_agent.get(llm_tool_name)
        if agent_id == "__orchestrator__":
            from orchestrator import agentic_creation
            return await agentic_creation.handle_meta_tool(
                self, tool_name, args, user_id=user_id, chat_id=chat_id, websocket=websocket
            )
        if agent_id == "__scheduler__":
            # Feature 030 — scheduling meta-tool: validation + consent card
            # only; creation happens in the schedule_decision ui_event.
            from orchestrator import scheduling_chat
            return await scheduling_chat.handle_meta_tool(
                self, tool_name, args, user_id=user_id, chat_id=chat_id, websocket=websocket
            )
        if agent_id == "__memory__":
            # 030 — memory meta-tools: execute immediately (PHI-gated), no card.
            from orchestrator import memory_chat
            return await memory_chat.handle_meta_tool(
                self, tool_name, args, user_id=user_id, chat_id=chat_id, websocket=websocket
            )
        if agent_id == "__desktop_codegen__":
            # 039 — desktop codegen: surface the generated code + a verified
            # download card for the Windows coding-agent .exe.
            from orchestrator import desktop_codegen
            return await desktop_codegen.handle_meta_tool(
                self, tool_name, args, user_id=user_id, chat_id=chat_id, websocket=websocket
            )
        if agent_id == "__subtasks__":
            # 056 US4 — planner decomposition into bounded isolated sub-tasks.
            # A sub-task may use only the tools THIS turn offered (never a
            # superset — FR-020); the handler's own gates and the sub-turns'
            # full gate stacks do the rest.
            from orchestrator import subtasks as _st
            args["_parent_tools"] = sorted(
                {t for t in (tool_to_unqualified or {}).values()
                 if not str(tool_to_agent.get(t, "")).startswith("__")}
                or {t for t, a in (tool_to_agent or {}).items()
                    if not str(a).startswith("__")})
            return await _st.handle_meta_tool(
                self, tool_name, args, user_id=user_id, chat_id=chat_id, websocket=websocket
            )

        # 056 US3 (FR-017): the FULL gate stack runs in the shared authorizer
        # so single, parallel, and chained dispatch refuse identically. Gate
        # logic lives there; this path keeps its immediate per-call delivery.
        auth = await self._authorize_and_prepare(
            websocket, agent_id, tool_name, args, chat_id, user_id,
            stream_params=stream_params, parent_token=parent_token,
            initiating_agent_id=initiating_agent_id)
        if isinstance(auth, GateRefusal):
            if auth.render_components:
                if auth.render_target:
                    await self.send_ui_render(
                        websocket, auth.render_components, target=auth.render_target)
                else:
                    await self.send_ui_render(websocket, auth.render_components)
            # T035/FR-012: a permission-denied dispatch returns BEFORE
            # ToolDispatchAudit, so for a USER agent (untrusted-at-the-boundary)
            # the denial would otherwise leave no audit row. Scope to user agents
            # so the shared gate's behavior for built-ins/public is unchanged.
            try:
                if await asyncio.to_thread(self._is_user_agent, agent_id):
                    await self._audit_user_agent(
                        user_id, f"tool.{tool_name}.denied",
                        "User-agent tool dispatch denied at the permission gate.",
                        agent_id, outcome="failure")
            except Exception:
                logger.debug("058: denial audit skipped", exc_info=True)
            return auth.response
        args = auth.args
        stream_params = auth.stream_params
        cap_job_id = auth.cap_job_id

        # Audit: record the tool dispatch (in_progress → success/failure)
        from audit.hooks import ToolDispatchAudit
        claims = self.ui_sessions.get(websocket) if websocket is not None else None
        if claims is None and websocket is not None:
            # 056 US2 (FR-014): machine turns carry a synthetic machine-context
            # marker on their virtual socket — record them attributed, never
            # dropped as "legacy".
            claims = getattr(websocket, "machine_claims", None)
        if initiating_agent_id and isinstance(claims, dict):
            # 056 US1: a hop's tool-call rows name the ACTING agent via the
            # RFC 8693 act claim while the human stays the actor_user_id.
            claims = {**claims, "act": {"sub": f"agent:{initiating_agent_id}"}}
        async with ToolDispatchAudit(
            claims=claims,
            agent_id=agent_id,
            tool_name=tool_name,
            chat_id=chat_id,
            correlation_id=auth.hop_correlation_id,
            args_meta={k: v for k, v in args.items() if not (isinstance(k, str) and k.startswith("_"))},
        ) as _audit_ctx:
            result = await self._execute_with_retry(websocket, agent_id, tool_name, args)
            if result and result.error:
                _audit_ctx.set_outcome("failure", str(result.error.get("message", ""))[:1000])
            elif result is None:
                _audit_ctx.set_outcome("interrupted", "no result returned")
            else:
                _audit_ctx.set_outputs_meta({"has_ui_components": bool(result.ui_components)})
            # Feature 004: propagate the audit correlation_id onto the response
            # so the caller can tag every produced UI component with the
            # originating dispatch's id. The frontend's component_feedback
            # flow uses this to scope a user's feedback to a specific dispatch.
            if result is not None:
                try:
                    result.correlation_id = _audit_ctx.correlation_id
                except Exception:
                    pass

        # Record the call's output taint so it propagates through the chain.
        # The output's trust = min(source trust, input trust): an untrusted
        # web/third-party source taints its output, and any tool that consumed
        # untrusted input passes the taint on (laundering survives an
        # intermediate hop). Flag-gated; best-effort (never affects the call).
        if user_id and result is not None and result.error is None:
            try:
                from orchestrator import taint as _taint
                if _taint.taint_enabled():
                    tracker = self._taint_tracker(chat_id)
                    src = _taint.classify_source(agent_id, tool_name)
                    inp = tracker.effective_trust_of_args(args)
                    tracker.record_output(result.ui_components, src, inp)
            except Exception:
                logger.debug("taint: output record failed", exc_info=True)

        # 015-external-ai-agents: release cap if the dispatch errored or returned
        # nothing — there will be no terminal ToolProgress to do it. Successful
        # long-running starts keep the slot held; the JobPoller's terminal
        # ToolProgress will release it via the handler in handle_agent_messages.
        if cap_job_id and (result is None or (result is not None and result.error)):
            try:
                await self.concurrency_cap.release(user_id, agent_id, cap_job_id)
            finally:
                self._pending_cap_entries.pop(cap_job_id, None)
            await self._release_hop_cap_slot(cap_job_id)

        # Hook: POST_TOOL_USE or POST_TOOL_FAILURE
        if flags.is_enabled("hook_system"):
            post_event = HookEvent.POST_TOOL_FAILURE if (result and result.error) else HookEvent.POST_TOOL_USE
            await self.hooks.emit(HookContext(
                event=post_event,
                user_id=user_id or "",
                agent_id=agent_id or "",
                tool_name=tool_name,
                tool_args=args,
                tool_result=result.result if result else None,
                error=result.error.get("message") if (result and result.error) else None,
            ))

        # Don't render tool results immediately — the caller (handle_chat_message)
        # collects the round's components and either runs the adaptive UI
        # designer over them or flat-appends them to the workspace (029).
        if result and result.error:
            # Errors are still shown immediately so the user knows something went wrong
            err_msg = result.error.get('message', 'Unknown error')
            await self.send_ui_render(websocket, [
                Alert(message=f"Tool '{tool_name}' failed: {err_msg}", variant="error").to_dict()
            ], target="chat")

            # Auto-fix: if this is a draft agent, attempt to fix the tool
            # error automatically. 030: the draft check now gates the STATUS
            # too — previously every errored live tool flashed a misleading
            # "Auto-fixing..." even though auto_fix only acts on drafts.
            # The draft lookup is a sync DB read — off the loop thread (052).
            if (agent_id and hasattr(self, 'lifecycle_manager')
                    and await asyncio.to_thread(
                        self.lifecycle_manager._get_draft_by_agent_id, agent_id)):
                try:
                    await self._safe_send(websocket, json.dumps({
                        "type": "chat_status", "status": "fixing",
                        "message": f"Auto-fixing tool '{tool_name}'..."
                    }))
                    fixed = await self.lifecycle_manager.auto_fix_tool_error(
                        agent_id, tool_name, err_msg, websocket
                    )
                    if fixed:
                        logger.info(f"Auto-fix attempted for draft agent {agent_id} tool '{tool_name}'")
                        await self.send_ui_render(websocket, [
                            Alert(message=f"Auto-fix applied for '{tool_name}'. Agent restarted — try again.", variant="info").to_dict()
                        ])
                    await self._safe_send(websocket, json.dumps({
                        "type": "chat_status", "status": "thinking",
                        "message": "Continuing after fix..."
                    }))
                except Exception as e:
                    logger.warning(f"Auto-fix failed for {agent_id}: {e}")
                    await self._safe_send(websocket, json.dumps({
                        "type": "chat_status", "status": "thinking",
                        "message": "Continuing..."
                    }))

        return result

    # Scopes considered safe for concurrent execution (read-only operations)
    _PARALLEL_SAFE_SCOPES = frozenset({"tools:read", "tools:search"})
    _MAX_PARALLEL_CONCURRENCY = 10

    def _chain_budget_for(self, chat_id: Optional[str]):
        """The turn's global chain budget (056 FR-021), lazily created. Bounds
        cumulative depth, total hop count, and wall clock across ALL nesting in
        the turn (interactive or machine).

        Chat-keyed budgets reset at each turn start (``handle_chat_message``
        pops ``chat_id``). A chat-less dispatch (``chat_id`` None) has no turn
        boundary to reset on, so its shared ``_global`` budget is recreated
        once it exhausts — otherwise a single process would refuse every
        chat-less hop forever after the first wall-clock window elapses."""
        from orchestrator.chain_authority import ChainBudget
        key = chat_id or "_global"
        budget = self._chain_budgets.get(key)
        if budget is None or (chat_id is None and budget.exhausted()):
            budget = ChainBudget(turn_id=f"turn_{_uuid.uuid4().hex[:8]}", chat_id=chat_id)
            self._chain_budgets[key] = budget
        return budget

    def _register_dispatch_context(self, request_id: str, agent_id: str,
                                   args: Dict, ui_websocket) -> None:
        """Record the orchestrator-side context of an in-flight dispatch
        (056 US1). A mediated hop from the executing agent resolves user,
        chat, UI socket, and — critically — the PARENT delegation authority
        from this record, never from anything the agent presents (FR-001)."""
        try:
            from orchestrator import delegation as _dg
            token = args.get("_delegation_token") if isinstance(args, dict) else None
            parent_payload = _dg.decode_token_payload(token) if token else None
            if parent_payload is not None:
                # Give a production Keycloak first-hop token the depth-0 actor
                # claim it lacks, so a later child minted off it verifies as a
                # complete chain. The actor is THIS dispatch's agent — resolved
                # from our own record, never from anything the agent presents.
                parent_payload = _dg.normalize_hop_parent(parent_payload, agent_id)
            self._dispatch_context[request_id] = {
                "agent_id": agent_id,
                "user_id": args.get("user_id") if isinstance(args, dict) else None,
                "chat_id": args.get("session_id") if isinstance(args, dict) else None,
                "ui_websocket": ui_websocket,
                "parent_token": parent_payload,
            }
        except Exception:  # never let bookkeeping break a dispatch
            logger.debug("dispatch context registration failed", exc_info=True)

    async def _record_hop_audit(self, *, operation: str, outcome: str,
                                parent: Optional[Dict], child: Optional[Dict],
                                callee_agent_id: str, tool_name: str,
                                chat_id: Optional[str], correlation_id: str,
                                detail: Optional[str] = None,
                                requested_scopes=None, granted_scopes=None) -> None:
        """Append one hop provenance record to the hash-chained audit under
        the ``delegation`` event class (056 T018, FR-026). Paired records
        (``delegation.hop.mint`` / ``delegation.hop.enforce``) share the hop's
        correlation_id with the hop's own tool-call pair, so a full chain is
        reconstructable from the log alone. Carries actor/scope/depth
        metadata only — NEVER token bytes (FR-028)."""
        from audit.recorder import get_recorder, now_utc
        from audit.schemas import AuditEventCreate
        from orchestrator import delegation as _dg
        rec = get_recorder()
        if rec is None:
            return
        base = _dg.delegation_chain_audit_record(
            parent or {}, child or {}, operation=operation, tool=tool_name)
        human = base.get("human_authorizer")
        acting = base.get("acting_agent") or f"agent:{callee_agent_id}"
        if not human:
            logger.warning("hop audit skipped: no human authorizer (op=%s tool=%s)",
                           operation, tool_name)
            return
        inputs_meta: Dict[str, Any] = {
            "parent_actor": base.get("parent_actor"),
            "acting_agent": acting,
            "delegation_depth": base.get("delegation_depth"),
            "actor_chain": base.get("actor_chain"),
        }
        if requested_scopes is not None:
            inputs_meta["requested_scopes"] = sorted(requested_scopes)[:16]
        if granted_scopes is not None:
            inputs_meta["granted_scopes"] = sorted(granted_scopes)[:16]
        try:
            await rec.record(AuditEventCreate(
                actor_user_id=human,
                auth_principal=acting,
                agent_id=callee_agent_id,
                event_class="delegation",
                action_type=f"delegation.hop.{operation}",
                description=f"delegation hop {operation}: {tool_name} on {callee_agent_id}",
                conversation_id=chat_id,
                correlation_id=correlation_id,
                outcome=outcome,
                outcome_detail=detail,
                inputs_meta=inputs_meta,
                started_at=now_utc(),
            ))
        except Exception:
            logger.debug("hop audit record failed", exc_info=True)

    async def _mint_child_for_hop(self, parent_token: Dict, agent_id: str,
                                  tool_name: str, user_id: str,
                                  chat_id: Optional[str]):
        """Mint + verify the child authority for one hop (056 T015/T016/T017).

        Returns ``(encoded_child_token, hop_correlation_id)`` on success or a
        :class:`GateRefusal` (per-call, fail-closed, audited — never
        session-terminating). The child satisfies the 048 invariants: scopes =
        intersection(parent, requested), expiry ≤ parent, depth = parent + 1
        (refused past the bound), actor chain terminating at the human. An
        empty intersection against a non-empty request refuses outright
        (FR-005, D3) rather than dispatching a do-nothing token. Credentials
        are NEVER carried on the token — the per-(user, callee) credential
        injection already ran in the authorizer (FR-008).
        """
        from audit.recorder import make_correlation_id
        from orchestrator import delegation as _dg
        corr = make_correlation_id()

        # Requested scopes for the callee — the same (user, callee) resolution
        # the flat single-hop mint uses: the callee's non-blocked, user-enabled
        # tools plus the user's enabled scope-level claims.
        card = self.agent_cards.get(agent_id)
        agent_flags = self.security_flags.get(agent_id, {})

        def _scope_reads():
            allowed = []
            for skill in (card.skills if card else []):
                if skill.id in agent_flags and agent_flags[skill.id].get("blocked"):
                    continue
                if not self.tool_permissions.is_tool_allowed(user_id, agent_id, skill.id):
                    continue
                allowed.append(skill.id)
            return allowed, self.tool_permissions.get_enabled_scope_names(user_id, agent_id)

        allowed_tools, enabled_scopes = await asyncio.to_thread(_scope_reads)
        requested = list(enabled_scopes or []) + [f"tool:{t}" for t in allowed_tools]

        def _refusal(message_text: str) -> GateRefusal:
            alert = Alert(message=message_text, variant="warning")
            return GateRefusal(
                response=MCPResponse(
                    error={"message": message_text, "retryable": False}),
                render_components=[alert.to_dict()],
                hop_audited=True)  # this path emitted its own hop record

        # Fail closed BEFORE minting if the child-signing key is unset in
        # production posture — never sign a child delegation with the committed
        # dev constant (Constitution X). Checked up front so no partial audit
        # trail (mint/enforce) is emitted for a hop that cannot be signed.
        try:
            _dg._child_signing_key()
        except _dg.DelegationConfigError:
            logger.error("delegation.hop.mint refused signing_key_unset callee=%s tool=%s chat=%s",
                         agent_id, tool_name, chat_id)
            await self._record_hop_audit(
                operation="mint", outcome="failure", parent=parent_token,
                child=None, callee_agent_id=agent_id, tool_name=tool_name,
                chat_id=chat_id, correlation_id=corr, detail="signing_key_unset",
                requested_scopes=requested)
            return _refusal(
                "Chained hop refused: the delegation signing key is not "
                "configured (set DELEGATION_CHILD_SIGNING_KEY).")

        try:
            child = _dg.mint_child_delegation(parent_token, agent_id, requested)
        except _dg.DelegationDepthExceeded as exc:
            logger.warning("delegation.hop.mint refused depth_exceeded callee=%s tool=%s chat=%s",
                           agent_id, tool_name, chat_id)
            await self._record_hop_audit(
                operation="mint", outcome="failure", parent=parent_token,
                child=None, callee_agent_id=agent_id, tool_name=tool_name,
                chat_id=chat_id, correlation_id=corr, detail="depth_exceeded",
                requested_scopes=requested)
            return _refusal(
                f"Chained hop refused: delegation depth limit reached ({exc}).")

        granted = (child.get("scope") or "").split()
        if requested and not granted:
            logger.warning("delegation.hop.mint refused empty_intersection callee=%s tool=%s chat=%s",
                           agent_id, tool_name, chat_id)
            await self._record_hop_audit(
                operation="mint", outcome="failure", parent=parent_token,
                child=child, callee_agent_id=agent_id, tool_name=tool_name,
                chat_id=chat_id, correlation_id=corr, detail="empty_intersection",
                requested_scopes=requested, granted_scopes=granted)
            return _refusal(
                f"Chained hop refused: none of the scopes '{tool_name}' needs "
                f"are within the parent delegation's authority.")

        await self._record_hop_audit(
            operation="mint", outcome="in_progress", parent=parent_token,
            child=child, callee_agent_id=agent_id, tool_name=tool_name,
            chat_id=chat_id, correlation_id=corr,
            requested_scopes=requested, granted_scopes=granted)

        required_scope = ""
        try:
            required_scope = self.tool_permissions.get_tool_scope(agent_id, tool_name) or ""
        except Exception:
            required_scope = ""
        ok, reason = _dg.authorize_chained_tool_call(child, tool_name, required_scope)
        if not ok:
            logger.warning("delegation.hop.enforce refused reason=%r callee=%s tool=%s chat=%s",
                           reason, agent_id, tool_name, chat_id)
            await self._record_hop_audit(
                operation="enforce", outcome="failure", parent=parent_token,
                child=child, callee_agent_id=agent_id, tool_name=tool_name,
                chat_id=chat_id, correlation_id=corr, detail=reason,
                granted_scopes=granted)
            return _refusal(f"Chained hop refused: {reason}.")

        await self._record_hop_audit(
            operation="enforce", outcome="success", parent=parent_token,
            child=child, callee_agent_id=agent_id, tool_name=tool_name,
            chat_id=chat_id, correlation_id=corr, granted_scopes=granted)
        logger.info("delegation.hop.minted callee=%s tool=%s depth=%s chat=%s corr=%s",
                    agent_id, tool_name, child.get("delegation_depth"), chat_id, corr)
        return _dg.encode_delegation_payload(child), corr

    async def _handle_agent_hop_request(self, websocket, msg: "AgentHopRequest") -> None:
        """Mediate one agent-initiated hop (056 US1, FR-001/FR-003/FR-029).

        Resolves the initiator's user/chat/UI-socket/parent-authority from the
        orchestrator's OWN dispatch record (never agent-supplied), charges the
        turn's global chain budget, and re-enters ``execute_single_tool`` so
        the hop passes the FULL single-path gate stack under a freshly minted
        child delegation. Every refusal is per-call and honest (an error
        ``MCPResponse``, never a teardown — FR-028); refusals that carry a
        resolvable authority (a mint/enforce/budget/reserved-callee refusal)
        are also audited to the hash chain, while the two pre-authority
        refusals — an unknown/spoofed parent and the flag-off inert path —
        are log-only (there is no derivable principal to attribute them to).
        Reserved ``__``-pseudo-agent ids are refused before dispatch, so the
        meta-tool exemption is structurally unavailable to hops (FR-003/FR-018).
        """
        from orchestrator import delegation as _dg
        hop_id = msg.request_id or ""
        callee = msg.callee_agent_id or ""
        tool_name = msg.tool_name or ""
        initiator = msg.initiator_agent_id or ""

        async def _refuse(message_text: str, *, ctx=None, detail=None, parent=None):
            if detail is not None:
                from audit.recorder import make_correlation_id
                await self._record_hop_audit(
                    operation="mint", outcome="failure",
                    parent=parent or {"sub": (ctx or {}).get("user_id")},
                    child=None, callee_agent_id=callee, tool_name=tool_name,
                    chat_id=(ctx or {}).get("chat_id"),
                    correlation_id=make_correlation_id(), detail=detail)
            await self._deliver_hop_response(websocket, hop_id, MCPResponse(
                request_id=hop_id,
                error={"message": message_text, "retryable": False}))

        try:
            if not _dg.recursive_delegation_enabled():
                # Flag off ⇒ the chaining seam does not exist (FR-009).
                return await _refuse(
                    "Agent-to-agent chaining is disabled (FF_RECURSIVE_DELEGATION off).")
            ctx = self._dispatch_context.get(msg.parent_request_id or "")
            if ctx is None or ctx.get("agent_id") != initiator:
                logger.warning(
                    "delegation.hop refused unknown_parent initiator=%s callee=%s tool=%s",
                    initiator, callee, tool_name)
                return await _refuse(
                    "Hop refused: no active parent dispatch for this request.",
                    detail="unknown_parent")
            if not callee or callee.startswith("__"):
                logger.warning(
                    "delegation.hop refused reserved_callee initiator=%s callee=%s",
                    initiator, callee)
                return await _refuse(
                    f"Hop refused: '{callee}' is not a dispatchable agent.",
                    ctx=ctx, detail="reserved_callee",
                    parent=ctx.get("parent_token"))
            parent = ctx.get("parent_token")
            if not parent:
                # No parent authority to attenuate — refuse rather than mint
                # ambient authority (FR-001); dev-mode hops exercise real
                # minting too (D17.2), so the refusal is identical everywhere.
                logger.warning(
                    "delegation.hop refused no_parent_authority initiator=%s callee=%s",
                    initiator, callee)
                return await _refuse(
                    "Hop refused: the initiating dispatch carries no delegated "
                    "authority to attenuate.", ctx=ctx, detail="no_parent_authority")
            budget = self._chain_budget_for(ctx.get("chat_id"))
            reason = budget.charge(_dg._token_depth(parent) + 1)
            if reason is not None:
                logger.warning(
                    "delegation.hop refused budget_stop reason=%s initiator=%s callee=%s chat=%s",
                    reason, initiator, callee, ctx.get("chat_id"))
                return await _refuse(
                    f"Hop refused: chain budget exhausted ({reason}).",
                    ctx=ctx, detail=f"budget_stop:{reason}", parent=parent)

            logger.info(
                "delegation.hop.request initiator=%s callee=%s tool=%s chat=%s depth=%s",
                initiator, callee, tool_name, ctx.get("chat_id"),
                _dg._token_depth(parent) + 1)
            from types import SimpleNamespace
            tc = SimpleNamespace(function=SimpleNamespace(
                name=tool_name, arguments=json.dumps(dict(msg.arguments or {}))))
            resp = await self.execute_single_tool(
                ctx.get("ui_websocket"), tc, {tool_name: callee},
                ctx.get("chat_id"), user_id=ctx.get("user_id"),
                parent_token=parent, initiating_agent_id=initiator)
            if resp is None:
                resp = MCPResponse(
                    request_id=hop_id,
                    error={"message": "hop produced no result", "retryable": True})
            else:
                resp = await self._scan_hop_payload(
                    resp, initiator=initiator, callee=callee, tool_name=tool_name,
                    ctx=ctx, parent=parent)
            await self._deliver_hop_response(websocket, hop_id, resp)
        except Exception as exc:
            logger.exception("hop mediation failed (hop=%s)", hop_id)
            await self._deliver_hop_response(websocket, hop_id, MCPResponse(
                request_id=hop_id,
                error={"message": f"hop mediation error: {exc}", "retryable": False}))

    async def _scan_hop_payload(self, resp: MCPResponse, *, initiator: str,
                                callee: str, tool_name: str, ctx: Dict,
                                parent: Optional[Dict]) -> MCPResponse:
        """Scan a hop RESULT before it enters the initiating agent's context
        (056 FR-007/D11).

        Chaining turns one agent's output into another agent's input — exactly
        the multi-agent flow the C-S14 scanner was built for, where it has been
        advisory (logged, delivered anyway) on the tool path. On an inter-agent
        hop it ENFORCES: a finding quarantines the payload (it is NOT delivered
        upstream), records an audited reason, and returns an honest error to the
        requesting agent, which can work around it. Fail-open on scanner error —
        a broken scanner must not break dispatch.

        Deliberately NOT gated on ``FF_MAS_DEFENSE`` (which gates the advisory
        tool-path scan): scanning inter-agent payloads is a core guarantee of
        chaining, so it rides the chaining flag itself — with
        ``FF_RECURSIVE_DELEGATION`` off no hop exists and nothing changes.
        """
        from audit.recorder import make_correlation_id
        from orchestrator import mas_defense
        if resp.error is not None:
            return resp
        try:
            # Scan BOTH channels the hop delivers upstream (result AND
            # ui_components) — _deliver_hop_response forwards both, so a marker
            # in either reaches the initiating agent's context.
            findings = []
            if resp.result is not None:
                findings += mas_defense.scan_message(resp.result)
            if resp.ui_components:
                findings += mas_defense.scan_message(resp.ui_components)
        except Exception:  # pragma: no cover — scanner is pure/stdlib
            logger.debug("hop payload scan failed — delivering", exc_info=True)
            return resp
        if not findings:
            return resp
        markers = sorted({f.marker for f in findings})
        logger.warning(
            "delegation.hop.quarantine initiator=%s callee=%s tool=%s markers=%s chat=%s",
            initiator, callee, tool_name, markers, ctx.get("chat_id"))
        await self._record_hop_audit(
            operation="enforce", outcome="failure", parent=parent, child=None,
            callee_agent_id=callee, tool_name=tool_name,
            chat_id=ctx.get("chat_id"), correlation_id=make_correlation_id(),
            detail=f"quarantined: injection markers {', '.join(markers)[:120]}")
        msg = (f"The result of '{tool_name}' was quarantined: it contained "
               f"prompt-injection markers and was not delivered.")
        return MCPResponse(error={"message": msg, "retryable": False})

    async def _deliver_hop_response(self, initiator_ws, hop_id: str,
                                    resp: MCPResponse) -> None:
        """Deliver a mediated hop's outcome to the initiating agent (056 US1).

        In-process initiators awaited a future registered on their loopback
        socket — resolve it directly. Networked initiators receive an
        ``agent_hop_response`` frame over their existing control socket."""
        futures = getattr(initiator_ws, "_hop_futures", None)
        if isinstance(futures, dict):
            fut = futures.pop(hop_id, None)
            if fut is not None and not fut.done():
                fut.set_result(resp)
                return
        send = getattr(initiator_ws, "send", None)
        if send is None:
            logger.warning("no route to deliver hop response %s", hop_id)
            return
        try:
            frame = AgentHopResponse(request_id=hop_id, response={
                "result": resp.result,
                "error": resp.error,
                "ui_components": resp.ui_components,
            })
            await send(frame.to_json())
        except Exception:
            logger.warning("hop response delivery failed for %s", hop_id, exc_info=True)

    async def _release_hop_cap_slot(self, cap_job_id: str) -> None:
        """Release the initiating agent's slot of a dual-charged hop (056
        FR-019). No-op when the job was not a hop. Called from every site
        that releases the executing agent's slot."""
        entry = self._hop_cap_entries.pop(cap_job_id, None)
        if entry:
            u_id, a_id = entry
            try:
                await self.concurrency_cap.release(u_id, a_id, cap_job_id)
            except Exception:
                logger.debug("hop cap release failed", exc_info=True)

    async def _execute_with_retry_audited(self, websocket, agent_id, tool_name, args, chat_id=None, user_id=None):
        """Dispatch a tool with the SAME ToolDispatchAudit start/end events,
        taint output-recording, and POST-tool hooks as the single-tool path
        (feature 040 / FR-032; 056 US3 gate-parity).

        The parallel-tool path historically called ``_execute_with_retry``
        directly, emitting no ``agent_tool_call`` audit rows AND skipping both
        the taint output record and the POST_TOOL_USE/FAILURE hooks that
        ``execute_single_tool`` runs after a dispatch — so an untrusted value
        produced in a multi-call round was never marked tainted (multi-hop
        exfil-laundering defense bypassed) and post-tool hook side effects
        (interaction collector → personalization/knowledge, admin handlers)
        were dropped. Routing every parallel dispatch through this wrapper makes
        those three behaviors identical to the single path.
        """
        from audit.hooks import ToolDispatchAudit
        claims = self.ui_sessions.get(websocket) if websocket is not None else None
        async with ToolDispatchAudit(
            claims=claims,
            agent_id=agent_id,
            tool_name=tool_name,
            chat_id=chat_id,
            args_meta={k: v for k, v in (args or {}).items() if not (isinstance(k, str) and k.startswith("_"))},
            invocation_channel=(claims or {}).get("_invocation_channel"),
        ) as _audit_ctx:
            result = await self._execute_with_retry(websocket, agent_id, tool_name, args)
            if result and result.error:
                _audit_ctx.set_outcome("failure", str(result.error.get("message", ""))[:1000])
            elif result is None:
                _audit_ctx.set_outcome("interrupted", "no result returned")
            else:
                _audit_ctx.set_outputs_meta({"has_ui_components": bool(result.ui_components)})
            if result is not None:
                try:
                    result.correlation_id = _audit_ctx.correlation_id
                except Exception:
                    pass

        # Taint output-recording — parity with the single path. Untrusted tool
        # output taints its result; without this a later sink (send/egress)
        # sees a multi-call-round value as trusted. Flag-gated, best-effort.
        if user_id and result is not None and result.error is None:
            try:
                from orchestrator import taint as _taint
                if _taint.taint_enabled():
                    tracker = self._taint_tracker(chat_id)
                    src = _taint.classify_source(agent_id, tool_name)
                    inp = tracker.effective_trust_of_args(args)
                    tracker.record_output(result.ui_components, src, inp)
            except Exception:
                logger.debug("taint: output record failed (parallel)", exc_info=True)

        # POST_TOOL_USE / POST_TOOL_FAILURE — parity with the single path.
        if flags.is_enabled("hook_system"):
            post_event = HookEvent.POST_TOOL_FAILURE if (result and result.error) else HookEvent.POST_TOOL_USE
            await self.hooks.emit(HookContext(
                event=post_event,
                user_id=user_id or "",
                agent_id=agent_id or "",
                tool_name=tool_name,
                tool_args=args,
                tool_result=result.result if result else None,
                error=result.error.get("message") if (result and result.error) else None,
            ))
        return result

    async def execute_mcp_tool(
        self,
        *,
        claims: Dict[str, Any],
        user_id: str,
        agent_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> MCPResponse:
        """Execute one MCP call through the complete shared authorization stack."""

        # This hashable sentinel supplies verified claims to policy, LLM-context,
        # delegation, and audit seams without creating a UI transport or retaining
        # the inbound bearer. It is removed in all outcomes.
        invocation = object()
        safe_claims = dict(claims)
        safe_claims.pop("_raw_token", None)
        safe_claims["_invocation_channel"] = "mcp"
        self.ui_sessions[invocation] = safe_claims
        auth = None
        result: Optional[MCPResponse] = None
        try:
            auth = await self._authorize_and_prepare(
                invocation,
                agent_id,
                tool_name,
                dict(arguments),
                None,
                user_id,
                stream_params=dict(arguments),
            )
            if isinstance(auth, GateRefusal):
                return auth.response
            result = await self._execute_with_retry_audited(
                invocation,
                agent_id,
                tool_name,
                auth.args,
                chat_id=None,
                user_id=user_id,
            )
            if result is None:
                return MCPResponse(
                    error={"message": "Tool returned no response", "retryable": True}
                )
            return result
        finally:
            cap_job_id = getattr(auth, "cap_job_id", None)
            if cap_job_id and (result is None or result.error):
                try:
                    await self.concurrency_cap.release(user_id, agent_id, cap_job_id)
                finally:
                    self._pending_cap_entries.pop(cap_job_id, None)
                await self._release_hop_cap_slot(cap_job_id)
            self.ui_sessions.pop(invocation, None)

    async def execute_parallel_tools(self, websocket, tool_calls, tool_to_agent: Dict, chat_id: str = None, user_id: str = None, tool_to_unqualified: Optional[Dict[str, str]] = None) -> List[Optional[MCPResponse]]:
        """Execute multiple tool calls with concurrency safety.

        When tool_concurrency_safety is enabled, read-only tools (tools:read,
        tools:search scopes) run in parallel while write/system tools run serially
        after the parallel batch completes.  This prevents race conditions when
        two write tools target the same agent.
        """
        # Phase 1: Prepare all tool calls (args, permissions, credentials)
        prepared = []  # list of (index, tc, tool_name, agent_id, args | None, error_coro | None)
        separately_rendered_refusals: set[int] = set()

        for idx, tc in enumerate(tool_calls):
            # Same qualified→unqualified resolution as the single-tool path:
            # an LLM-emitted name like "forecaster-1__submit_dataset" is mapped
            # back to the bare skill id "submit_dataset" before dispatch so the
            # owning agent receives the name it registered.
            llm_tool_name = tc.function.name
            if tool_to_unqualified and llm_tool_name in tool_to_unqualified:
                tool_name = tool_to_unqualified[llm_tool_name]
            else:
                tool_name = llm_tool_name
            agent_id = tool_to_agent.get(llm_tool_name)
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except json.JSONDecodeError as _json_err:
                # Hard-gate malformed tool-call arguments instead of silently
                # dispatching with empty args (silent repair / parser loss).
                # Surface the parse failure back to the model so it can retry
                # with valid JSON — mirrors the security/permission error coro.
                _pj_msg = (f"The arguments for '{tool_name}' were not valid "
                           f"JSON ({str(_json_err).splitlines()[0]}). Re-emit "
                           f"the tool call with well-formed JSON arguments.")
                logger.warning("tool_arg_parse_fail(parallel) tool=%s user=%s err=%s",
                               tool_name, user_id, _json_err.msg)

                async def _arg_err(msg=_pj_msg):
                    return MCPResponse(error={"message": msg, "retryable": True})
                prepared.append((idx, tc, tool_name, agent_id, None, _arg_err()))
                continue

            # 055 US2: LLM-authored params as written (see execute_single_tool).
            stream_params = dict(args)

            # agent_id resolved above (before the JSON parse) so the parse-fail
            # error path can include it in the prepared tuple.

            # Feature 027/030/039 — meta-tools dispatch directly, with the SAME
            # four reserved pseudo-agent branches as the single path (056 US3
            # T008/FR-018 — previously only __orchestrator__ worked here). The
            # exemption stays limited to these reserved ids; real-agent calls
            # (and therefore chained hops) can never reach a meta-tool handler.
            if agent_id == "__orchestrator__":
                from orchestrator import agentic_creation
                prepared.append((idx, tc, tool_name, agent_id, None,
                                 agentic_creation.handle_meta_tool(
                                     self, tool_name, args, user_id=user_id,
                                     chat_id=chat_id, websocket=websocket)))
                continue
            if agent_id == "__scheduler__":
                from orchestrator import scheduling_chat
                prepared.append((idx, tc, tool_name, agent_id, None,
                                 scheduling_chat.handle_meta_tool(
                                     self, tool_name, args, user_id=user_id,
                                     chat_id=chat_id, websocket=websocket)))
                continue
            if agent_id == "__memory__":
                from orchestrator import memory_chat
                prepared.append((idx, tc, tool_name, agent_id, None,
                                 memory_chat.handle_meta_tool(
                                     self, tool_name, args, user_id=user_id,
                                     chat_id=chat_id, websocket=websocket)))
                continue
            if agent_id == "__desktop_codegen__":
                from orchestrator import desktop_codegen
                prepared.append((idx, tc, tool_name, agent_id, None,
                                 desktop_codegen.handle_meta_tool(
                                     self, tool_name, args, user_id=user_id,
                                     chat_id=chat_id, websocket=websocket)))
                continue
            if agent_id == "__subtasks__":
                from orchestrator import subtasks as _st
                # Sub-task tool-scoping filters by UNQUALIFIED skill id
                # (handle_chat_message: ``skill.id not in selected_tools``), so
                # ``_parent_tools`` must carry unqualified ids — identical to
                # the single-tool path. Passing the qualified LLM names
                # (``tool_to_agent`` keys like ``forecaster-1__submit_dataset``)
                # made every collision-qualified tool silently unmatched and
                # dropped from the sub-task's allow-list.
                args["_parent_tools"] = sorted(
                    {t for t in (tool_to_unqualified or {}).values()
                     if not str(tool_to_agent.get(t, "")).startswith("__")}
                    or {t for t, a in (tool_to_agent or {}).items()
                        if not str(a).startswith("__")})
                prepared.append((idx, tc, tool_name, agent_id, None,
                                 _st.handle_meta_tool(
                                     self, tool_name, args, user_id=user_id,
                                     chat_id=chat_id, websocket=websocket)))
                continue

            # 056 US3 (T007/FR-017): the FULL single-path gate stack via the
            # shared authorizer. The parallel path previously applied only
            # creds/security/permission/no-agent and skipped policy, taint,
            # supervisor, HITL, the RFC 8693 delegation mint (dispatching
            # UNSCOPED where the single path refuses fail-closed), the 054
            # LLM-credential surfacing, PRE_TOOL_USE, and the concurrency
            # cap. Refusals keep this path's batched delivery: the refusal
            # response carries its error and is rendered with the error batch
            # below, exactly like any other failed parallel call.
            auth = await self._authorize_and_prepare(
                websocket, agent_id, tool_name, args, chat_id, user_id,
                stream_params=stream_params)
            if isinstance(auth, GateRefusal):
                if auth.render_components:
                    if auth.render_target:
                        await self.send_ui_render(
                            websocket,
                            auth.render_components,
                            target=auth.render_target,
                        )
                    else:
                        await self.send_ui_render(websocket, auth.render_components)
                    separately_rendered_refusals.add(idx)
                async def _refused(resp=auth.response):
                    return resp
                prepared.append((idx, tc, tool_name, agent_id, None, _refused()))
                continue

            prepared.append((idx, tc, tool_name, agent_id, auth.args, None))

        if not prepared:
            return []

        # Phase 2: Partition into parallel-safe vs serial based on scope
        use_concurrency_safety = flags.is_enabled("tool_concurrency_safety")

        parallel_items = []  # (idx, tool_name, coro)
        serial_items = []    # (idx, tool_name, agent_id, args)
        error_items = []     # (idx, coro)

        for idx, tc, tool_name, agent_id, args, err_coro in prepared:
            if err_coro is not None:
                error_items.append((idx, err_coro))
            elif use_concurrency_safety:
                scope = self.tool_permissions.get_tool_scope(agent_id, tool_name)
                if scope in self._PARALLEL_SAFE_SCOPES:
                    parallel_items.append((idx, tool_name, self._execute_with_retry_audited(websocket, agent_id, tool_name, args, chat_id, user_id)))
                else:
                    serial_items.append((idx, tool_name, agent_id, args))
            else:
                # 056 US3: audit parity holds on this branch too (040 FR-032
                # routed only the concurrency-safety branches through the
                # audited wrapper; flag-off dispatches were unaudited).
                parallel_items.append((idx, tool_name, self._execute_with_retry_audited(websocket, agent_id, tool_name, args, chat_id, user_id)))

        # Collect results in original order
        results_by_idx: Dict[int, Any] = {}

        # Execute error items immediately
        for idx, coro in error_items:
            results_by_idx[idx] = await coro

        # Execute parallel-safe tools concurrently (capped)
        if parallel_items:
            sem = asyncio.Semaphore(self._MAX_PARALLEL_CONCURRENCY)
            async def _sem_wrap(coro):
                async with sem:
                    return await coro
            par_results = await asyncio.gather(
                *[_sem_wrap(coro) for _, _, coro in parallel_items],
                return_exceptions=True
            )
            for (idx, _, _), res in zip(parallel_items, par_results):
                results_by_idx[idx] = res

        # Execute serial (write/system) tools one at a time
        for idx, tool_name, agent_id, args in serial_items:
            try:
                results_by_idx[idx] = await self._execute_with_retry_audited(websocket, agent_id, tool_name, args, chat_id, user_id)
            except Exception as e:
                results_by_idx[idx] = e

        if serial_items:
            logger.info(f"Concurrency safety: {len(parallel_items)} parallel, {len(serial_items)} serial")

        # Reassemble in original order
        ordered = [results_by_idx.get(i) for i in range(len(tool_calls))]
        tool_names = [tc.function.name for tc in tool_calls]

        # 056 US3: the authorizer now acquires the concurrency cap for
        # long-running parallel dispatches — release slots for errored/absent
        # results here, since no terminal ToolProgress will arrive to do it
        # (mirrors the single path's release-on-error).
        args_by_idx = {p[0]: p[4] for p in prepared if p[4] is not None}
        for _idx, _res in enumerate(ordered):
            _errored = (
                _res is None or isinstance(_res, Exception)
                or (getattr(_res, "error", None) is not None))
            if not _errored:
                continue
            _cap_id = (args_by_idx.get(_idx) or {}).get("_cap_job_id")
            if not _cap_id:
                continue
            _entry = self._pending_cap_entries.pop(_cap_id, None)
            if _entry:
                try:
                    await self.concurrency_cap.release(_entry[0], _entry[1], _cap_id)
                except Exception:
                    logger.debug("cap release failed", exc_info=True)
            await self._release_hop_cap_slot(_cap_id)

        results = ordered
        
        # Process results — don't render here, caller batches into collapsible
        final_results = []
        error_components = []
        
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                err_res = MCPResponse(error={"message": str(result)})
                final_results.append(err_res)
                error_components.append(Alert(message=f"Tool error: {str(result)}", variant="error").to_dict())
            else:
                final_results.append(result)
                if result and result.error and i not in separately_rendered_refusals:
                    error_components.append(Alert(message=f"Tool '{tool_names[i]}' failed: {result.error.get('message')}", variant="error").to_dict())

        # Only render errors immediately — successful results are batched by caller
        if error_components:
            await self.send_ui_render(websocket, error_components)

        # Auto-fix: attempt to fix draft agent tool errors
        if hasattr(self, 'lifecycle_manager'):
            for i, result in enumerate(final_results):
                if result and result.error:
                    t_name = tool_names[i] if i < len(tool_names) else None
                    a_id = tool_to_agent.get(t_name) if t_name else None
                    # 030: status only when auto-fix can actually act (drafts).
                    # The lookup is a sync DB read — off the loop thread (052).
                    if a_id and await asyncio.to_thread(
                            self.lifecycle_manager._get_draft_by_agent_id, a_id):
                        try:
                            await self._safe_send(websocket, json.dumps({
                                "type": "chat_status", "status": "fixing",
                                "message": f"Auto-fixing tool '{t_name}'..."
                            }))
                            await self.lifecycle_manager.auto_fix_tool_error(
                                a_id, t_name, result.error.get('message', ''), websocket
                            )
                            await self.send_ui_render(websocket, [
                                Alert(message=f"Auto-fix applied for '{t_name}'. Agent restarted — try again.", variant="info").to_dict()
                            ])
                            await self._safe_send(websocket, json.dumps({
                                "type": "chat_status", "status": "thinking",
                                "message": "Continuing after fix..."
                            }))
                        except Exception as e:
                            logger.warning(f"Auto-fix failed for {a_id}: {e}")
                            await self._safe_send(websocket, json.dumps({
                                "type": "chat_status", "status": "thinking",
                                "message": "Continuing..."
                            }))

        return final_results

    async def _execute_with_retry(
        self, websocket, agent_id: str, tool_name: str, args: Dict,
        max_retries: int = None
    ) -> Optional[MCPResponse]:
        """Execute a tool call with up to max_retries attempts.

        On retryable errors, sends status updates to the UI and waits
        with exponential backoff before trying again.
        """
        if max_retries is None:
            max_retries = self.MAX_RETRIES

        last_result = None

        for attempt in range(1, max_retries + 1):
            result = await self.execute_tool_and_wait(
                agent_id, tool_name, args,
                timeout=TOOL_TIMEOUT_OVERRIDES.get(tool_name, 30.0),
                ui_websocket=websocket)
            last_result = result

            # Success: no error at all
            if result and not result.error:
                if attempt > 1:
                    logger.info(f"Tool '{tool_name}' succeeded on attempt {attempt}/{max_retries}")
                return result

            # Check if error is retryable
            is_retryable = True
            error_msg = "Unknown error"
            if result and result.error:
                is_retryable = result.error.get("retryable", True)
                error_msg = result.error.get("message", "Unknown error")

            if not is_retryable:
                logger.info(f"Tool '{tool_name}' failed with non-retryable error: {error_msg}")
                return result

            # Retryable error — try again if attempts remain
            if attempt < max_retries:
                base = self.RETRY_BACKOFF[attempt - 1] if attempt - 1 < len(self.RETRY_BACKOFF) else 2.0
                # ±20% jitter to avoid thundering-herd when concurrent tool
                # calls fail against the same upstream (mirrors
                # stream_manager.compute_backoff's jitter pattern).
                backoff = base * random.uniform(0.8, 1.2)
                logger.warning(
                    f"Tool '{tool_name}' failed (attempt {attempt}/{max_retries}): {error_msg}. "
                    f"Retrying in {backoff}s..."
                )
                # Notify UI about the retry
                try:
                    await self._safe_send(websocket, json.dumps({
                        "type": "chat_status",
                        "status": "retrying",
                        "message": f"Tool '{tool_name.replace('_', ' ').title()}' failed. "
                                   f"Retrying... (attempt {attempt + 1}/{max_retries})"
                    }))
                except Exception:
                    pass  # Don't let status notification failure break retry logic

                await asyncio.sleep(backoff)
            else:
                logger.error(
                    f"Tool '{tool_name}' failed after {max_retries} attempts: {error_msg}"
                )

        return last_result

    async def execute_tool_and_wait(self, agent_id: str, tool_name: str, args: Dict, timeout: float = 30.0, ui_websocket=None) -> Optional[MCPResponse]:
        """Send an MCP tool call to an agent and wait for the response.

        Strategy: Always try WebSocket first (fastest, bidirectional), then
        fall back to A2A JSON-RPC if WebSocket is unavailable or fails.

        Feature 014: every tool call is recorded as a persistent step entry
        via :class:`orchestrator.chat_steps.ChatStepRecorder` so users see
        what was called in the chat. Recording is purely observational —
        a missing recorder (e.g. no UI websocket) is not an error.
        """
        # Feature 014: look up the active per-turn recorder for this UI
        # websocket so we can stamp start/complete/error around the call.
        recorder = self._chat_recorders.get(id(ui_websocket)) if ui_websocket is not None else None
        step_id = None
        if recorder is not None:
            try:
                step_id = await recorder.start("tool_call", tool_name, args)
            except Exception:  # pragma: no cover — defensive
                logger.debug("recorder.start failed", exc_info=True)
                step_id = None

        try:
            result = await self._dispatch_tool_call(agent_id, tool_name, args, timeout, ui_websocket)
            if recorder is not None and step_id is not None:
                # R6: if the step was cancelled mid-flight, drop the result
                # silently so the assistant reply does not include it.
                if recorder.is_terminal(step_id):
                    logger.info(
                        "tool_call result discarded (step terminal)",
                        extra={"step_id": step_id, "tool_name": tool_name},
                    )
                else:
                    if result is not None and result.error:
                        await recorder.error(step_id, result.error.get("message", "tool error"))
                    else:
                        # Surface a small result preview if present.
                        preview = result.result if (result is not None and result.result is not None) else None
                        await recorder.complete(step_id, preview)
            return result
        except Exception as exc:
            if recorder is not None and step_id is not None and not recorder.is_terminal(step_id):
                try:
                    await recorder.error(step_id, exc)
                except Exception:  # pragma: no cover — defensive
                    pass
            raise

    async def _dispatch_tool_call(self, agent_id: str, tool_name: str, args: Dict, timeout: float, ui_websocket) -> Optional[MCPResponse]:
        """Internal: actually dispatch the tool call (in-process → WebSocket → A2A)."""
        # 058 (honest-offline, FR-011): a user-created agent runs on the owner's
        # desktop and connects inward over the tunnel — it has no server-reachable
        # URL. When its host is closed it is simply offline; short-circuit to a
        # prompt, honest offline response rather than the reconnect/A2A dance
        # (which would hang or mislead for a NAT'd user agent). Only queried on the
        # already-disconnected path, so it adds no cost to live dispatches.
        if agent_id not in self.agents and agent_id not in self.local_agents:
            try:
                from orchestrator import user_agents as _ua
                if await asyncio.to_thread(_ua.is_user_agent, self.history.db, agent_id):
                    return MCPResponse(
                        request_id=f"req_{tool_name}_{_uuid.uuid4().hex}",
                        error={"message": (
                            f"'{agent_id}' is offline — it runs on your device and its "
                            f"client isn't connected right now. Reopen the client that "
                            f"hosts it and try again."),
                            "retryable": False, "offline": True})
            except Exception:
                logger.debug("user-agent offline check failed", exc_info=True)
        # Feature 040 (US1): bundled first-party agents run IN-PROCESS — no
        # network hop. Selected by a positive registry check; external A2A
        # agents and draft subprocesses fall through to the paths below.
        if agent_id in self.local_agents:
            return await self._execute_in_process(
                agent_id, tool_name, args, timeout, ui_websocket=ui_websocket)
        # Try WebSocket first
        if agent_id in self.agents:
            result = await self._execute_via_websocket(agent_id, tool_name, args, timeout, ui_websocket=ui_websocket)
            if result and not (result.error and result.error.get("retryable")):
                return result
            # WebSocket failed with a retryable error — fall back to A2A if available
            if agent_id in self.a2a_clients:
                logger.info(f"WebSocket call failed for {agent_id}, falling back to A2A")
                return await self._execute_via_a2a(agent_id, tool_name, args, timeout)
            return result

        # No WebSocket connection — try A2A
        if agent_id in self.a2a_clients:
            return await self._execute_via_a2a(agent_id, tool_name, args, timeout)

        # Agent has a known URL but no active connection — attempt WebSocket reconnect then A2A
        if agent_id in self.agent_urls:
            base_url = self.agent_urls[agent_id]
            logger.info(f"Agent {agent_id} disconnected, attempting WebSocket reconnect to {base_url}")
            try:
                await self.discover_agent(base_url)
                if agent_id in self.agents:
                    return await self._execute_via_websocket(agent_id, tool_name, args, timeout, ui_websocket=ui_websocket)
            except Exception as e:
                logger.debug(f"WebSocket reconnect failed for {agent_id}: {e}")

            # WebSocket reconnect failed — try A2A discovery as fallback
            logger.info(f"WebSocket reconnect failed for {agent_id}, attempting A2A fallback")
            try:
                await self.discover_a2a_agent(base_url, notify_ui=False)
                if agent_id in self.a2a_clients:
                    return await self._execute_via_a2a(agent_id, tool_name, args, timeout)
            except Exception as e:
                logger.debug(f"A2A fallback discovery failed for {agent_id}: {e}")

        return MCPResponse(
            request_id=f"req_{tool_name}_{_uuid.uuid4().hex}",
            error={"message": f"Agent {agent_id} not connected via WebSocket or A2A", "retryable": False},
        )

    async def _execute_via_websocket(self, agent_id: str, tool_name: str, args: Dict, timeout: float = 30.0, ui_websocket=None) -> Optional[MCPResponse]:
        """Execute a tool call via WebSocket (internal agents)."""
        projected_socket = self.agents.get(agent_id)
        if bool(
            getattr(projected_socket, "is_fenced_user_agent_tunnel", False)
        ):
            return await self._execute_via_personal_runtime(
                projected_socket,
                tool_name,
                args,
                timeout=timeout,
                ui_websocket=ui_websocket,
            )
        # Cryptographically-random, collision-free request id. A time-based id
        # (``req_<tool>_<ms>``) collided when two same-tool calls landed in one
        # millisecond — resolving the WRONG pending future — and, being sent to
        # the agent in the dispatch, was GUESSABLE: a malicious agent could
        # forge a hop's ``parent_request_id`` to resolve another dispatch's
        # authority from ``_dispatch_context`` (confused-deputy). uuid4 is
        # os.urandom-backed, so neither collision nor guessing is feasible.
        request_id = f"req_{tool_name}_{_uuid.uuid4().hex}"

        request = MCPRequest(
            request_id=request_id,
            method="tools/call",
            params={"name": tool_name, "arguments": args},
            protocol_version=MCP_PROTOCOL_VERSION,
            caller_capabilities={},
            caller_info={"name": "AstralDeep Orchestrator", "version": "1.0.0"},
        )

        # Create a future for the response
        future = asyncio.get_event_loop().create_future()
        self.pending_requests[request_id] = future
        # The agent this request is SENT to — a response from any other socket is
        # dropped (see _response_is_from_dispatch_target). Guarded so a test
        # double reusing this method with its own `self` keeps working.
        _targets = getattr(self, "_pending_request_agent", None)
        if _targets is not None:
            _targets[request_id] = agent_id
        # 056 US1: record this dispatch so a mediated hop from the executing
        # agent resolves its context/authority against OUR record.
        self._register_dispatch_context(request_id, agent_id, args, ui_websocket)

        # Register UI socket for progress forwarding
        if ui_websocket and flags.is_enabled("progress_streaming"):
            self.pending_ui_sockets[request_id] = ui_websocket

        try:
            agent_ws = self.agents[agent_id]
            await agent_ws.send(request.to_json())
            logger.info(f"Sent tool call (WS): {tool_name} → {agent_id}")

            result = await asyncio.wait_for(future, timeout=timeout)
            return result

        except asyncio.TimeoutError:
            logger.error(f"Tool call timed out: {tool_name}")
            return MCPResponse(request_id=request_id,
                               error={"message": "Tool call timed out", "retryable": True})
        except Exception as e:
            logger.error(f"Tool execution error: {e}")
            return MCPResponse(request_id=request_id,
                               error={"message": str(e), "retryable": True})
        finally:
            self.pending_requests.pop(request_id, None)
            (getattr(self, "_pending_request_agent", None) or {}).pop(request_id, None)
            self.pending_ui_sockets.pop(request_id, None)
            self._dispatch_context.pop(request_id, None)

    async def _execute_via_personal_runtime(
        self,
        socket: Any,
        tool_name: str,
        args: Dict[str, Any],
        *,
        timeout: float,
        ui_websocket: Any,
    ) -> MCPResponse:
        """Assign, send, and settle one exact personal-agent runtime call."""

        owner_user_id = socket.owner_sub
        agent_id = socket.agent_id
        request_generation = _uuid.uuid4()
        parent_context = _CONNECTION_OPERATION_CONTEXT.get() or {}
        parent_fence = parent_context.get("execution_fence")
        parent_operation_id = (
            parent_fence.operation_id
            if isinstance(parent_fence, ExecutionFence)
            else None
        )
        claimed = await self._claim_personal_agent_operation(
            owner_user_id=owner_user_id,
            operation_kind="agent_runtime_request",
            idempotency_namespace="personal_agent_runtime_request",
            idempotency_key=str(request_generation),
            normalized_identity={
                "agent_id": agent_id,
                "tool_name": tool_name,
                "request_generation": str(request_generation),
            },
            admission_class=AdmissionClass.INTERACTIVE,
            parent_operation_id=parent_operation_id,
            request_generation=request_generation,
            wait_seconds=min(2.0, max(0.1, timeout)),
        )
        if claimed is None:
            return MCPResponse(
                request_id=str(request_generation),
                error={
                    "message": "Personal-agent capacity is temporarily unavailable",
                    "retryable": True,
                    "code": "capacity_exceeded",
                },
            )
        _owner, operation_claim = claimed
        try:
            authority = await asyncio.to_thread(
                self.personal_agent_runtime.get_current_online_authority,
                owner_user_id=owner_user_id,
                agent_id=agent_id,
            )
            if authority.fence != socket.runtime_fence:
                raise RuntimeError("personal-agent route projection is stale")
            request = await asyncio.to_thread(
                self.personal_agent_runtime.assign_request,
                authority.fence,
                operation_fence=operation_claim.fence,
                request_generation=str(request_generation),
            )
        except Exception:
            try:
                await self._call_work_admission(
                    self.work_admission.terminalize,
                    operation_claim.fence,
                    state=OperationState.RETRYABLE,
                    terminal_code="agent_offline",
                    safe_summary="Personal agent is offline",
                    retry_after_ms=0,
                )
            except Exception:
                logger.debug("personal-agent call admission cleanup failed", exc_info=True)
            return MCPResponse(
                request_id=str(request_generation),
                error={
                    "message": "Personal agent is offline",
                    "retryable": True,
                    "offline": True,
                },
            )

        request_id = request.fence.request_id
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[Any] = loop.create_future()
        stop_renewal = asyncio.Event()
        renewal_task = asyncio.create_task(
            self._renew_personal_agent_operation_lease(
                operation_claim.fence,
                stop_renewal,
            ),
            name=f"personal-agent-lease-{request_id}",
        )
        self._personal_agent_request_waiters[request_id] = waiter
        self._register_dispatch_context(
            request_id, agent_id, args, ui_websocket
        )
        targets = getattr(self, "_pending_request_agent", None)
        if targets is None:
            targets = self._pending_request_agent = {}
        targets[request_id] = agent_id
        inner = {
            "type": "mcp_request",
            "request_id": request_id,
            "request_generation": request.fence.request_generation,
            "fence": authority.fence.to_dict(),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": args},
        }
        try:
            await socket.send_fenced(inner)
            async with asyncio.timeout(timeout):
                return await asyncio.shield(waiter)
        except TimeoutError:
            try:
                await asyncio.to_thread(
                    self.personal_agent_runtime.settle_request,
                    request.fence,
                    state="retryable",
                    terminal_code="request_timeout",
                    result_digest=None,
                )
            except Exception:
                logger.debug("personal-agent timeout was already settled", exc_info=True)
            return MCPResponse(
                request_id=request_id,
                error={"message": "Tool call timed out", "retryable": True},
            )
        except Exception as exc:
            try:
                await asyncio.to_thread(
                    self.personal_agent_runtime.settle_request,
                    request.fence,
                    state="retryable",
                    terminal_code="host_lost",
                    result_digest=None,
                )
            except Exception:
                logger.debug("personal-agent send failure was already settled", exc_info=True)
            return MCPResponse(
                request_id=request_id,
                error={"message": str(exc), "retryable": True},
            )
        finally:
            stop_renewal.set()
            renewal_task.cancel()
            await asyncio.gather(renewal_task, return_exceptions=True)
            self._personal_agent_request_waiters.pop(request_id, None)
            (getattr(self, "_pending_request_agent", None) or {}).pop(
                request_id, None
            )
            self._dispatch_context.pop(request_id, None)

    async def _execute_in_process(self, agent_id: str, tool_name: str, args: Dict, timeout: float = 30.0, ui_websocket=None) -> Optional[MCPResponse]:
        """Execute a tool call against a built-in agent running IN-PROCESS (feature 040).

        Runs the agent's own ``handle_mcp_request`` with a
        :class:`~shared.local_transport.LoopbackSocket` whose frames route back
        through ``handle_agent_message`` — so the same request-id↔future
        correlation, progress fan-out, streaming, in-agent credential
        decryption, and ``_runtime`` injection all apply with no network hop.

        NOTE on confidentiality: for an in-process built-in the agent handler
        runs inside the orchestrator OS process, so decrypted ECIES plaintext
        DOES transiently exist in this process's memory — the confidentiality
        boundary here is encryption-at-rest in the DB, not a separate process
        (unlike the networked A2A path). To keep that plaintext from leaking
        back into the orchestrator-owned argument dict, the request is built
        against a deep copy of ``args`` and the copy is scrubbed afterwards.

        The whole gate stack (permission/policy/taint/audit/concurrency) wraps
        this call upstream in ``execute_single_tool`` and is unchanged.
        """
        import copy
        from shared.local_transport import LoopbackSocket
        # Cryptographically-random, collision-free request id (see
        # _execute_via_websocket) — this id keys both ``pending_requests`` and
        # ``_dispatch_context``, the record a mediated hop resolves authority
        # from, so it must be neither collidable nor guessable.
        request_id = f"req_{tool_name}_{_uuid.uuid4().hex}"
        # Private copy so in-agent credential decryption never writes plaintext
        # back into the caller's args dict (callers may retain/audit it).
        call_args = copy.deepcopy(args)
        request = MCPRequest(
            request_id=request_id,
            method="tools/call",
            params={"name": tool_name, "arguments": call_args},
            protocol_version=MCP_PROTOCOL_VERSION,
            caller_capabilities={},
            caller_info={"name": "AstralDeep Orchestrator", "version": "1.0.0"},
        )
        future = asyncio.get_event_loop().create_future()
        self.pending_requests[request_id] = future
        _targets = getattr(self, "_pending_request_agent", None)
        if _targets is not None:
            _targets[request_id] = agent_id      # dispatch target (see above)
        # 056 US1: record this dispatch so a mediated hop from the executing
        # agent resolves its context/authority against OUR record. Registered
        # against the ORIGINAL args (the deep copy is scrubbed in-agent).
        self._register_dispatch_context(request_id, agent_id, args, ui_websocket)
        if ui_websocket and flags.is_enabled("progress_streaming"):
            self.pending_ui_sockets[request_id] = ui_websocket

        def _on_handler_done(task: "asyncio.Task"):
            # The agent handler resolves the future itself via the loopback's
            # MCPResponse frame. If it raised before sending one (it should not —
            # mcp_server.process_request catches tool errors and returns an
            # error response), resolve with a retryable error so the awaiter
            # never hangs until timeout.
            try:
                exc = task.exception()
            except asyncio.CancelledError:
                exc = RuntimeError("in-process tool task cancelled")
            if exc is not None and not future.done():
                logger.error("In-process tool handler crashed for %s: %s", tool_name, exc)
                future.set_result(MCPResponse(
                    request_id=request_id,
                    error={"message": str(exc), "retryable": True}))

        try:
            agent = self.local_agents[agent_id]
            loopback = LoopbackSocket(self, agent_id)
            task = asyncio.create_task(agent.handle_mcp_request(loopback, request))
            task.add_done_callback(_on_handler_done)
            logger.info(f"Dispatched tool call (in-process): {tool_name} → {agent_id}")
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(f"In-process tool call timed out: {tool_name}")
            return MCPResponse(request_id=request_id,
                               error={"message": "Tool call timed out", "retryable": True})
        except Exception as e:
            logger.error(f"In-process tool execution error: {e}")
            return MCPResponse(request_id=request_id,
                               error={"message": str(e), "retryable": True})
        finally:
            # Scrub decrypted credential plaintext from the private copy.
            try:
                if isinstance(call_args, dict):
                    call_args.pop("_credentials", None)
            except Exception:
                pass
            self.pending_requests.pop(request_id, None)
            (getattr(self, "_pending_request_agent", None) or {}).pop(request_id, None)
            self.pending_ui_sockets.pop(request_id, None)
            self._dispatch_context.pop(request_id, None)

    async def _execute_via_a2a(self, agent_id: str, tool_name: str, args: Dict, timeout: float = 30.0) -> Optional[MCPResponse]:
        """Execute a tool call via A2A JSON-RPC (external agents).

        Posts a hand-rolled JSON-RPC `message/send` request to the agent's /a2a
        endpoint so the per-call delegation token can be forwarded as a Bearer
        Authorization header. Avoids the v1.0 SDK Client which routes auth
        through interceptors rather than per-call metadata.
        """
        import uuid
        import httpx
        from google.protobuf.json_format import ParseDict, MessageToDict
        from a2a.types import Message as A2AMessage, Role, Task, Message as A2AMsg
        from shared.a2a_bridge import make_data_part, a2a_response_to_mcp_response

        request_id = f"a2a_{tool_name}_{_uuid.uuid4().hex}"

        base_url = self.a2a_clients.get(agent_id) or self.agent_urls.get(agent_id)
        if not base_url:
            return MCPResponse(
                request_id=request_id,
                error={"message": f"No A2A endpoint registered for {agent_id}", "retryable": False},
            )
        a2a_url = base_url if base_url.rstrip("/").endswith("/a2a") else f"{base_url.rstrip('/')}/a2a"

        clean_args = {k: v for k, v in args.items() if not k.startswith("_")}
        if args.get("_credentials_encrypted"):
            clean_args["_credentials"] = args["_credentials"]
            clean_args["_credentials_encrypted"] = args["_credentials_encrypted"]

        msg = A2AMessage(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_USER,
            parts=[make_data_part({
                "method": "tools/call",
                "name": tool_name,
                "arguments": clean_args,
            })],
        )

        headers = {"Content-Type": "application/json"}
        delegation_token = args.get("_delegation_token")
        if delegation_token:
            headers["Authorization"] = f"Bearer {delegation_token}"

        jsonrpc_payload = {
            "jsonrpc": "2.0",
            "method": "message/send",
            "id": request_id,
            "params": {"message": MessageToDict(msg, preserving_proto_field_name=True)},
        }

        try:
            logger.info(f"Sent tool call (A2A): {tool_name} → {agent_id}")
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(a2a_url, json=jsonrpc_payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()

            if "error" in data:
                err = data["error"] if isinstance(data["error"], dict) else {"message": str(data["error"])}
                return MCPResponse(
                    request_id=request_id,
                    error={"message": err.get("message", "A2A error"), "retryable": False},
                )

            result = data.get("result")
            if isinstance(result, dict):
                # Try to parse the result as a Task; fall back to Message; else raw dict.
                try:
                    return a2a_response_to_mcp_response(ParseDict(result, Task()), request_id)
                except Exception:
                    pass
                try:
                    return a2a_response_to_mcp_response(ParseDict(result, A2AMsg()), request_id)
                except Exception:
                    pass
                return MCPResponse(request_id=request_id, result=result)

            if result is None:
                return MCPResponse(
                    request_id=request_id,
                    error={"message": "No response from A2A agent", "retryable": True},
                )
            return MCPResponse(request_id=request_id, result=str(result))

        except httpx.TimeoutException:
            logger.error(f"A2A tool call timed out: {tool_name}")
            return MCPResponse(request_id=request_id,
                               error={"message": "A2A tool call timed out", "retryable": True})
        except Exception as e:
            logger.error(f"A2A tool execution error: {e}")
            return MCPResponse(request_id=request_id,
                               error={"message": str(e), "retryable": True})

    def _delegation_required(self) -> bool:
        """Whether tool dispatch must refuse to proceed without a delegated token.

        Constitution VII mandates RFC 8693 delegated tokens for agents.
        Default: required in production posture (``ASTRAL_ENV`` unset or not
        ``development`` — the project's fail-closed convention), optional in
        development. ``DELEGATION_REQUIRED`` overrides either way.
        """
        override = os.getenv("DELEGATION_REQUIRED", "").strip().lower()
        if override in ("1", "true", "yes"):
            return True
        if override in ("0", "false", "no"):
            return False
        return os.getenv("ASTRAL_ENV", "").strip().lower() != "development"

    async def _delegation_denied_for_permissions(
        self, websocket, agent_id: str, user_id: str
    ) -> bool:
        """Whether a failed mint is this user's permissions, not the IdP's fault.

        An empty effective scope list means the user has granted this agent no
        runnable tool, so the exchange is refused locally (``no_enabled_scopes``)
        rather than sent to Keycloak with an empty ``scope``. The two causes
        need different refusals — telling a user to go fix a correctly
        configured realm is a dead end.

        A turn with no bound user token (an unconsented machine turn) is NOT a
        permissions fault even though its scope set is also empty: nothing was
        ever exchanged. Only consulted on the refusal path.
        """
        try:
            session = self.ui_sessions.get(websocket, {}) or {}
            if (
                not session.get("_raw_token")
                and session.get("_invocation_channel") != "mcp"
            ):
                return False
            scopes = await asyncio.to_thread(
                self.tool_permissions.get_enabled_scope_names, user_id, agent_id)
            return not scopes
        except Exception:
            return False

    async def _get_delegation_token(self, websocket, agent_id: str, user_id: str) -> Optional[str]:
        """Generate an RFC 8693 delegation token scoped to safe, allowed tools.

        The scope excludes system-blocked tools (from security review) and
        user-disabled tools (from permission manager), so the agent can only
        act within the constrained tool set.
        """
        try:
            card = self.agent_cards.get(agent_id)
            if not card:
                return None
            session = self.ui_sessions.get(websocket, {})
            if session.get("_invocation_channel") == "mcp":
                return await self._mint_mcp_delegation_token(
                    agent_id,
                    user_id,
                    session,
                )
            raw_token = session.get("_raw_token")
            if not raw_token:
                return None

            # Build the effective scope: only tools that pass BOTH checks
            agent_flags = self.security_flags.get(agent_id, {})

            def _scope_reads():
                """Per-skill permission reads + scope names off the event loop."""
                allowed = []
                for skill in card.skills:
                    # Exclude system-blocked
                    if skill.id in agent_flags and agent_flags[skill.id].get("blocked"):
                        continue
                    # Exclude user-disabled
                    if not self.tool_permissions.is_tool_allowed(user_id, agent_id, skill.id):
                        continue
                    allowed.append(skill.id)
                return allowed, self.tool_permissions.get_enabled_scope_names(user_id, agent_id)

            allowed_tools, enabled_scopes = await asyncio.to_thread(_scope_reads)

            result = await self.delegation.exchange_token_for_agent(
                raw_token, agent_id, allowed_tools, user_id, enabled_scopes
            )
            if "error" in result:
                if result.get("error") == "no_enabled_scopes":
                    # Not an IdP fault and not agent-wide: this user granted
                    # this agent nothing runnable. Per-user, so it must NOT
                    # mark the agent as "exchange failing" — that would
                    # downgrade a later, genuine realm failure to a debug line.
                    logger.warning(
                        "Delegation refused: user=%s has no enabled tool scopes for agent=%s",
                        user_id, agent_id)
                    return None
                # Feature 030: log loudly ONCE per agent instead of warning on
                # every call — a misconfigured realm previously produced an
                # identical warning per tool dispatch (pure noise) while the
                # dispatch itself proceeded unscoped.
                if not hasattr(self, "_delegation_failed_agents"):
                    self._delegation_failed_agents = set()
                if agent_id not in self._delegation_failed_agents:
                    self._delegation_failed_agents.add(agent_id)
                    logger.error(
                        "RFC 8693 token exchange failing for agent=%s (logged once; "
                        "see docs/keycloak_agent_delegation_setup.md): %s", agent_id, result)
                else:
                    logger.debug(f"Delegation token exchange failed for agent={agent_id}: {result}")
                return None
            return result.get("access_token")
        except Exception as e:
            logger.warning(f"Delegation token generation failed: {e}")
            return None

    async def _mint_mcp_delegation_token(
        self,
        agent_id: str,
        user_id: str,
        claims: Dict[str, Any],
    ) -> Optional[str]:
        """Mint downstream authority from verified claims, never bearer bytes."""

        from orchestrator import delegation as _dg

        card = self.agent_cards.get(agent_id)
        if not card:
            return None
        agent_flags = self.security_flags.get(agent_id, {})

        def _scope_reads():
            allowed = [
                skill.id
                for skill in card.skills
                if not agent_flags.get(skill.id, {}).get("blocked")
                and self.tool_permissions.is_tool_allowed(
                    user_id,
                    agent_id,
                    skill.id,
                )
            ]
            scopes = self.tool_permissions.get_enabled_scope_names(user_id, agent_id)
            return allowed, scopes

        allowed_tools, enabled_scopes = await asyncio.to_thread(_scope_reads)
        if not enabled_scopes:
            return None
        now = int(time.time())
        try:
            inbound_exp = int(claims.get("exp", now + 300))
        except (TypeError, ValueError):
            inbound_exp = now + 300
        payload = {
            "sub": user_id,
            "act": {"sub": f"agent:{agent_id}"},
            "scope": " ".join(
                list(enabled_scopes) + [f"tool:{tool}" for tool in allowed_tools]
            ),
            "iss": "astral-internal-delegation",
            "aud": self.delegation.agent_service_client_id,
            "iat": now,
            "exp": min(inbound_exp, now + 300),
            "delegation": True,
        }
        return _dg.encode_delegation_payload(payload)

    # =========================================================================
    # LIVE STREAMING SUBSCRIPTIONS
    # =========================================================================

    # =========================================================================
    # PUSH STREAMING (001-tool-stream-ui)
    # =========================================================================

    async def _validate_chat_ownership_for_stream(
        self, websocket, user_id: str, chat_id: str,
    ) -> bool:
        """Callback used by StreamManager to verify that ``chat_id`` belongs
        to ``user_id``. Reuses the existing history.get_chat ownership
        check that all other chat-scoped operations go through (off-loop —
        the 052 detector refuses sync DB calls on the event-loop thread).
        Returns True if the chat exists AND is owned by the user.
        """
        try:
            chat = await asyncio.to_thread(
                self.history.get_chat, chat_id, user_id=user_id)
            return chat is not None
        except Exception as e:
            logger.warning(f"chat ownership check failed: {e}")
            return False

    async def _dispatch_stream_request(
        self,
        agent_id: str,
        tool_name: str,
        args: Dict[str, Any],
        stream_id: str,
        user_id: Optional[str],
    ) -> str:
        """Dispatch a streaming tool call to an agent. Returns the
        ``request_id`` so StreamManager can populate ``_request_to_key``.

        Unlike ``_execute_via_websocket`` (the synchronous response path),
        this fire-and-forgets the request. The chunks arrive asynchronously
        as ``ToolStreamData`` messages and are routed back via
        ``handle_agent_message`` → ``stream_manager.handle_agent_chunk``.

        Per RFC 8693 (constitution VII), if user credentials are stored for
        this user/agent pair we inject them encrypted alongside the args
        before sending — exactly like the polling path.
        """
        if agent_id not in self.agents and agent_id not in self.local_agents:
            raise RuntimeError(f"agent {agent_id!r} is not connected")

        request_id = f"stream_{tool_name}_{_uuid.uuid4().hex}_{stream_id[-6:]}"

        # Inject per-user credentials (E2E encrypted — only the agent can decrypt)
        full_args = dict(args)
        if user_id and agent_id:
            try:
                creds = self.credential_manager.get_agent_credentials_encrypted(user_id, agent_id)
                if creds:
                    full_args["_credentials"] = creds
                    full_args["_credentials_encrypted"] = True
            except Exception as e:
                logger.debug(f"credential injection skipped for {agent_id}: {e}")

        request = MCPRequest(
            request_id=request_id,
            method="tools/call",
            params={
                "name": tool_name,
                "arguments": full_args,
                "_stream": True,
                "_stream_id": stream_id,
            },
            protocol_version=MCP_PROTOCOL_VERSION,
            caller_capabilities={},
            caller_info={"name": "AstralDeep Orchestrator", "version": "1.0.0"},
        )
        if agent_id in self.local_agents:
            # Feature 040 built-ins run in-process with no agent WS — the
            # loopback routes their ToolStreamData/End frames back through
            # handle_agent_message exactly like the networked path (this
            # dispatch had predated 040 and knew only self.agents, leaving
            # push streaming dead for every built-in).
            from shared.local_transport import LoopbackSocket
            agent = self.local_agents[agent_id]
            loopback = LoopbackSocket(self, agent_id)
            asyncio.create_task(agent.handle_mcp_request(loopback, request))
            logger.info(
                f"Dispatched streaming tool call (in-process): {tool_name} → "
                f"{agent_id} (stream_id={stream_id}, request_id={request_id})"
            )
            return request_id
        agent_ws = self.agents[agent_id]
        await agent_ws.send(request.to_json())
        logger.info(
            f"Dispatched streaming tool call: {tool_name} → {agent_id} "
            f"(stream_id={stream_id}, request_id={request_id})"
        )
        return request_id

    async def _cancel_stream_request(
        self, agent_id: str, request_id: str, stream_id: str,
    ) -> None:
        """Send a ``ToolStreamCancel`` to the agent for an in-flight stream.
        The agent's BaseA2AAgent loop closes the underlying generator and
        sends a final ``ToolStreamData`` with ``terminal: true``.
        """
        cancel_msg = ToolStreamCancel(request_id=request_id, stream_id=stream_id)
        if agent_id in self.local_agents:
            # In-process twin of the WS send: hand the cancel straight to the
            # agent's own handler (the loopback has no inbound channel).
            try:
                await self.local_agents[agent_id]._handle_stream_cancel(cancel_msg)
                logger.info(
                    f"Cancelled in-process stream: stream_id={stream_id} → {agent_id}"
                )
            except Exception as e:
                logger.warning(f"_cancel_stream_request (in-process) failed: {e}")
            return
        if agent_id not in self.agents:
            logger.debug(
                f"_cancel_stream_request: agent {agent_id} not connected, "
                f"nothing to cancel"
            )
            return
        try:
            await self.agents[agent_id].send(cancel_msg.to_json())
            logger.info(
                f"Sent ToolStreamCancel: stream_id={stream_id} → {agent_id}"
            )
        except Exception as e:
            logger.warning(f"_cancel_stream_request failed: {e}")

    async def _auto_subscribe_stream_artifacts(
        self, websocket, chat_id: Optional[str], user_id: Optional[str],
        tool_name: str, params: Dict[str, Any],
    ) -> None:
        """055 US2 (FR-009/FR-010): server-side subscription at streaming-tool
        dispatch.

        No client sends ``stream_subscribe`` unprompted (research D4), so a
        push-streamable tool dispatched from chat would stream to nobody.
        Subscribe the originating socket and every co-viewing socket of the
        chat here; the client ``stream_subscribe`` action stays valid for
        reattach (wire-contract §2). Called AFTER the dispatch gates pass, so
        no additional permission checks. Fail-open — any refusal leaves the
        turn on today's terminal-only delivery.
        """
        if websocket is None or not chat_id or not user_id:
            return
        # getattr: dispatch-focused test stubs build partial Orchestrators
        # without a stream_manager — fail-open applies to those too.
        if (getattr(self, "stream_manager", None) is None
                or not flags.is_enabled("stream_artifacts")
                or not flags.is_enabled("tool_streaming")):
            return
        tool_cfg = self._streamable_tools.get(tool_name)
        if not tool_cfg or tool_cfg.get("kind") != "push":
            return
        agent_id = tool_cfg["agent_id"]
        # The subscription identity must fingerprint the same params the
        # one-shot component is `_source_params`-stamped with — never the
        # injected private keys.
        clean_params = {k: v for k, v in params.items()
                        if not (isinstance(k, str) and k.startswith("_"))}
        # Originating socket first: it creates the subscription (dispatching
        # the streaming run to the agent); co-viewers attach (FR-009a dedup).
        targets = [websocket] + [
            ws for ws in self._sockets_on_chat(user_id, chat_id)
            if ws is not websocket
        ]
        for ws in targets:
            try:
                stream_id, attached = await self.stream_manager.subscribe(
                    ws=ws, user_id=user_id, chat_id=chat_id,
                    tool_name=tool_name, agent_id=agent_id,
                    params=clean_params, tool_metadata=tool_cfg,
                )
            except Exception as e:
                logger.info(
                    "stream_artifacts.auto_subscribe_skipped tool=%s agent=%s "
                    "chat=%s user=%s err=%s", tool_name, agent_id, chat_id,
                    user_id, e)
                continue
            reply = {
                "type": "stream_subscribed",
                "stream_id": stream_id,
                "tool_name": tool_name,
                "agent_id": agent_id,
                "session_id": chat_id,
                "max_fps": tool_cfg.get("max_fps", 30),
                "min_fps": tool_cfg.get("min_fps", 5),
                "attached": attached,
            }
            cid = self.stream_manager.component_id_for(stream_id)
            if cid is not None:
                reply["component_id"] = cid
            await self._safe_send(ws, json.dumps(reply))

    def _bridged_stream_subscription(self, stream_id: str):
        """The live bridged subscription for ``stream_id``, or None (flag off,
        unbridged legacy stream, or unknown id).

        Captured BEFORE the stream manager processes a chunk/end frame:
        terminal processing tears the record down, and the persist wrapper
        still needs its retention + identity afterwards.
        """
        if self.stream_manager is None or not flags.is_enabled("stream_artifacts"):
            return None
        sub = self.stream_manager.subscription_for_stream(stream_id)
        if sub is None or sub.bridged_component_id is None:
            return None
        return sub

    async def _persist_stream_terminal(self, sub) -> None:
        """055 US2 (FR-011): persist a bridged stream's terminal state as a
        normal workspace component under the identity every frame carried.

        Natural completion (``agent_end``) persists the retained last
        content-bearing chunk; a FAILED resolution persists an honest
        failed-state Alert under the SAME identity so reload shows the truth
        instead of silently dropping what the user watched. Non-terminal
        states (active/reconnecting/dormant) are left alone. Fail-open:
        persistence failures degrade to today's ephemeral behavior.
        """
        import copy
        from orchestrator.stream_manager import StreamState
        cid = sub.bridged_component_id
        if cid is None:
            return
        if sub.persist_done:
            return
        if sub.state is StreamState.FAILED:
            reason = (sub.state_reason or "error").replace("_", " ")
            components = [Alert(
                message=(f"The live stream from '{sub.tool_name}' ended with "
                         f"an error ({reason}) before completing."),
                variant="error").to_dict()]
        elif (sub.state is StreamState.STOPPED
                # dormant_ttl = abandonment; unsubscribe = the user closing an
                # indefinite live view (its only success-terminal). In both,
                # what streamed is still what persists.
                and sub.state_reason in ("agent_end", "dormant_ttl", "unsubscribe")
                and sub.retained_chunk is not None
                and sub.retained_chunk.components):
            components = copy.deepcopy(sub.retained_chunk.components)
        else:
            return
        for comp in components:
            if isinstance(comp, dict):
                # The agent SDK stamps every top-level id with the stream id;
                # dropping it here keeps the stream-scoped id from ever being
                # resolved as an author identity.
                if str(comp.get("id", "")).startswith("stream-"):
                    comp.pop("id", None)
                _tag_source(comp, sub.agent_id, sub.tool_name,
                            tool_params=sub.params)
        chat_id, user_id = sub.chat_id, sub.user_id
        try:
            async def _persist_terminal():
                staged_ops = await self.workspace.aupsert(
                    chat_id,
                    user_id,
                    components,
                    force_component_id=cid,
                )
                if staged_ops:
                    await self.workspace.asnapshot(
                        chat_id, user_id, cause="stream"
                    )
                return staged_ops

            ops = await self.run_detached_conversation_mutation(
                chat_id=chat_id,
                user_id=user_id,
                mutation=_persist_terminal,
            )
            sub.persist_done = True
        except Exception:
            logger.exception(
                "stream_artifacts.persist_failed stream=%s component=%s agent=%s "
                "tool=%s chat=%s user=%s",
                sub.stream_id, cid, sub.agent_id, sub.tool_name, chat_id, user_id)
            return
        if not ops:
            return
        try:
            await self.send_ui_upsert(None, chat_id, user_id, ops)
        except Exception:
            logger.debug("stream_artifacts.persist_fanout_failed stream=%s "
                         "component=%s chat=%s", sub.stream_id, cid, chat_id,
                         exc_info=True)
        try:
            from audit.hooks import record_workspace_event
            for op in ops:
                asyncio.create_task(record_workspace_event(
                    user_id=user_id,
                    action="component_updated" if not op.get("created") else "component_added",
                    chat_id=chat_id, component_id=op.get("component_id"),
                ))
        except Exception:
            logger.debug("workspace audit failed", exc_info=True)

    def _tool_security_blocked(self, agent_id: Optional[str], tool_name: str) -> bool:
        """True when a hard security-flag block is set for (agent, tool).

        Mirrors the dispatch-path block in ``_dispatch_tool_call`` so the
        streaming paths (subscribe / loop) cannot bypass a system-blocked tool.
        """
        flags = self.security_flags.get(agent_id, {}) if agent_id else {}
        return bool(agent_id and tool_name in flags and flags[tool_name].get("blocked"))

    async def _handle_push_stream_subscribe(
        self, websocket, session_id: Optional[str], payload: Dict, user_id: str
    ) -> None:
        """Handle a stream_subscribe action for a PUSH-streaming tool.

        Delegates to ``self.stream_manager.subscribe(...)``. Translates
        ``ValueError`` into a ``stream_error`` reply per
        contracts/protocol-messages.md §A6. On success replies with
        ``stream_subscribed``.

        US1 implementation: subscribe() actually creates a subscription and
        the agent dispatcher fires the request. agent_id is looked up from
        the orchestrator's _streamable_tools registry so the client doesn't
        need to know it (mirrors the legacy poll path).
        """
        tool_name = payload.get("tool_name", "")
        params = payload.get("params", {})
        chat_id = session_id or ""

        if not tool_name or not chat_id:
            await self._safe_send(websocket, json.dumps({
                "type": "stream_error",
                "request_action": "stream_subscribe",
                "session_id": session_id,
                "payload": {
                    "tool_name": tool_name,
                    "code": "params_invalid",
                    "message": "tool_name and session_id are required",
                },
            }))
            return

        tool_cfg = self._streamable_tools.get(tool_name)
        if tool_cfg is None or tool_cfg.get("kind") != "push":
            await self._safe_send(websocket, json.dumps({
                "type": "stream_error",
                "request_action": "stream_subscribe",
                "session_id": session_id,
                "payload": {
                    "tool_name": tool_name,
                    "code": "not_streamable",
                    "message": f"tool {tool_name!r} is not push-streamable",
                },
            }))
            return

        agent_id = tool_cfg["agent_id"]

        # Hard security-flag block (mirror the dispatch gate) — a system-blocked
        # tool must never be streamable, even when permissions would allow it.
        if self._tool_security_blocked(agent_id, tool_name):
            await self._safe_send(websocket, json.dumps({
                "type": "stream_error",
                "request_action": "stream_subscribe",
                "session_id": session_id,
                "payload": {
                    "tool_name": tool_name,
                    "code": "blocked",
                    "message": "tool is system-blocked by security review",
                },
            }))
            return

        # Permission check (mirrors legacy poll path)
        if not await asyncio.to_thread(
                self.tool_permissions.is_tool_allowed, user_id, agent_id, tool_name):
            await self._safe_send(websocket, json.dumps({
                "type": "stream_error",
                "request_action": "stream_subscribe",
                "session_id": session_id,
                "payload": {
                    "tool_name": tool_name,
                    "code": "unauthorized",
                    "message": "permission denied for this tool",
                },
            }))
            return

        try:
            stream_id, attached = await self.stream_manager.subscribe(
                ws=websocket,
                user_id=user_id,
                chat_id=chat_id,
                tool_name=tool_name,
                agent_id=agent_id,
                params=params,
                tool_metadata=tool_cfg,
            )
        except ValueError as e:
            await self._safe_send(websocket, json.dumps({
                "type": "stream_error",
                "request_action": "stream_subscribe",
                "session_id": session_id,
                "payload": {
                    "tool_name": tool_name,
                    "code": "params_invalid",
                    "message": str(e),
                },
            }))
            return

        # Success — reply with stream_subscribed including the FPS bounds and
        # the FR-009a `attached` flag so the client knows whether this was a
        # fresh subscribe or an attach to an existing deduplicated stream.
        cfg = self._streamable_tools.get(tool_name, {})
        reply = {
            "type": "stream_subscribed",
            "stream_id": stream_id,
            "tool_name": tool_name,
            "agent_id": agent_id,
            "session_id": session_id,
            "max_fps": cfg.get("max_fps", 30),
            "min_fps": cfg.get("min_fps", 5),
            "attached": attached,
        }
        # 055 US2: bridged streams carry the workspace identity from the ack
        # onward so the client keys the placeholder by it (wire-contract §2).
        cid = self.stream_manager.component_id_for(stream_id)
        if cid is not None:
            reply["component_id"] = cid
        await self._safe_send(websocket, json.dumps(reply))

    async def _handle_push_stream_unsubscribe(
        self, websocket, session_id: Optional[str], payload: Dict, user_id: str
    ) -> None:
        """Handle a stream_unsubscribe for a push-streamed subscription.

        Per FR-009a per-subscriber semantics, removing this websocket from
        the subscription's ``subscribers`` list does NOT necessarily stop
        the stream — only when the list becomes empty does the stream
        transition to STOPPED. The actual logic is in
        ``StreamManager.unsubscribe`` (US4 T066).
        """
        stream_id = payload.get("stream_id", "")
        if not stream_id:
            return  # silent: malformed unsubscribe is not worth a reply
        try:
            await self.stream_manager.unsubscribe(websocket, stream_id)
        except NotImplementedError:
            logger.debug(
                f"push stream_unsubscribe received but unsubscribe() not yet "
                f"implemented (stream_id={stream_id})"
            )
        except ValueError as e:
            await self._safe_send(websocket, json.dumps({
                "type": "stream_error",
                "request_action": "stream_unsubscribe",
                "session_id": session_id,
                "payload": {
                    "stream_id": stream_id,
                    "code": "unauthorized",
                    "message": str(e),
                },
            }))

    async def _handle_stream_subscribe(self, websocket, payload: Dict):
        """Subscribe a UI client to a live-streaming tool."""
        tool_name = payload.get("tool_name")
        interval = payload.get("interval_seconds")
        params = payload.get("params", {})

        if not tool_name or tool_name not in self._streamable_tools:
            await self._safe_send(websocket, json.dumps({
                "type": "stream_error", "tool_name": tool_name or "",
                "error": f"Tool '{tool_name}' is not available for streaming"
            }))
            return

        tool_cfg = self._streamable_tools[tool_name]
        agent_id = tool_cfg["agent_id"]

        # Hard security-flag block (mirror the dispatch gate).
        if self._tool_security_blocked(agent_id, tool_name):
            await self._safe_send(websocket, json.dumps({
                "type": "stream_error", "tool_name": tool_name,
                "error": "Tool is system-blocked by security review"
            }))
            return

        # Permission check
        user_id = self._get_user_id(websocket)
        if not await asyncio.to_thread(
                self.tool_permissions.is_tool_allowed, user_id, agent_id, tool_name):
            await self._safe_send(websocket, json.dumps({
                "type": "stream_error", "tool_name": tool_name,
                "error": "Permission denied for this tool"
            }))
            return

        # Clamp interval to tool's allowed range
        if interval is None:
            interval = tool_cfg["default_interval"]
        interval = max(tool_cfg["min_interval"], min(tool_cfg["max_interval"], interval))

        ws_id = id(websocket)

        # Enforce max subscription limit
        current_subs = self._stream_subs.get(ws_id, {})
        if tool_name not in current_subs and len(current_subs) >= self._MAX_STREAM_SUBSCRIPTIONS:
            await self._safe_send(websocket, json.dumps({
                "type": "stream_error", "tool_name": tool_name,
                "error": f"Maximum {self._MAX_STREAM_SUBSCRIPTIONS} concurrent streams exceeded"
            }))
            return

        # Cancel existing task for this tool if re-subscribing
        existing_task = self._stream_tasks.get(ws_id, {}).get(tool_name)
        if existing_task:
            existing_task.cancel()

        # Store subscription config
        self._stream_subs.setdefault(ws_id, {})[tool_name] = {
            "interval": interval, "params": params, "agent_id": agent_id,
        }

        # Create streaming task
        task = asyncio.create_task(
            self._stream_loop(websocket, tool_name, agent_id, interval, params)
        )
        self._stream_tasks.setdefault(ws_id, {})[tool_name] = task

        await self._safe_send(websocket, json.dumps({
            "type": "stream_subscribed", "tool_name": tool_name,
            "interval_seconds": interval,
        }))
        logger.info(f"Stream subscribed: user={user_id} tool={tool_name} interval={interval}s")

    async def _handle_stream_unsubscribe(self, websocket, payload: Dict):
        """Unsubscribe a UI client from a live-streaming tool."""
        tool_name = payload.get("tool_name")
        ws_id = id(websocket)

        task = self._stream_tasks.get(ws_id, {}).pop(tool_name, None)
        if task:
            task.cancel()
        self._stream_subs.get(ws_id, {}).pop(tool_name, None)

        await self._safe_send(websocket, json.dumps({
            "type": "stream_unsubscribed", "tool_name": tool_name,
        }))
        logger.info(f"Stream unsubscribed: tool={tool_name}")

    async def _handle_stream_list(self, websocket):
        """Return the list of active stream subscriptions for this client."""
        ws_id = id(websocket)
        subs = self._stream_subs.get(ws_id, {})
        items = [
            {"tool_name": name, "interval_seconds": cfg["interval"], "agent_id": cfg["agent_id"]}
            for name, cfg in subs.items()
        ]
        await self._safe_send(websocket, json.dumps({
            "type": "stream_list", "subscriptions": items,
        }))

    async def _stream_loop(self, websocket, tool_name: str, agent_id: str, interval: float, params: Dict):
        """Core streaming loop — periodically executes a tool and pushes results to the UI client."""
        user_id = self._get_user_id(websocket)
        while True:
            try:
                # A tool blocked mid-stream by security review must stop the loop.
                if self._tool_security_blocked(agent_id, tool_name):
                    await self._safe_send(websocket, json.dumps({
                        "type": "stream_error", "tool_name": tool_name,
                        "error": "Tool is system-blocked by security review"
                    }))
                    break

                # Re-check permission each iteration (user may revoke mid-stream)
                if not await asyncio.to_thread(
                        self.tool_permissions.is_tool_allowed, user_id, agent_id, tool_name):
                    await self._safe_send(websocket, json.dumps({
                        "type": "stream_error", "tool_name": tool_name,
                        "error": "Permission revoked"
                    }))
                    break

                # Execute tool via existing agent WebSocket channel
                result = await self._execute_via_websocket(agent_id, tool_name, dict(params), timeout=interval + 5)

                if result and not result.error:
                    # Tag components with source metadata (same as regular tool flow)
                    # Feature 004: also tag with correlation_id when available
                    # so streamed components are linkable to their dispatch.
                    stream_corr_id = getattr(result, "correlation_id", None)

                    def _tag(comp):
                        if not isinstance(comp, dict):
                            return
                        comp["_source_agent"] = agent_id
                        comp["_source_tool"] = tool_name
                        if stream_corr_id is not None:
                            comp["_source_correlation_id"] = stream_corr_id
                        for key in ("content", "children"):
                            nested = comp.get(key)
                            if isinstance(nested, list):
                                for child in nested:
                                    _tag(child)

                    tagged_components = list(result.ui_components or [])
                    for comp in tagged_components:
                        _tag(comp)
                    await self._safe_send(websocket, json.dumps({
                        "type": "stream_data",
                        "tool_name": tool_name,
                        "agent_id": agent_id,
                        "timestamp": time.time(),
                        "components": tagged_components,
                        "data": result.result or {},
                    }))
                elif result and result.error:
                    logger.warning(f"Stream tool error ({tool_name}): {result.error}")
                    # Don't break on transient errors; continue loop

                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Stream loop error for {tool_name}: {e}")
                await asyncio.sleep(interval)

        # Cleanup on exit
        ws_id = id(websocket)
        self._stream_tasks.get(ws_id, {}).pop(tool_name, None)
        self._stream_subs.get(ws_id, {}).pop(tool_name, None)

    def _cleanup_streams(self, websocket):
        """Cancel all streaming tasks for a disconnected websocket."""
        ws_id = id(websocket)
        for tool_name, task in self._stream_tasks.pop(ws_id, {}).items():
            task.cancel()
        self._stream_subs.pop(ws_id, None)

    # =========================================================================
    # UI HELPERS
    # =========================================================================

    def _voice_binding_runtime_issuer(self) -> VoiceControlBindingIssuer:
        issuer = self._voice_binding_issuer
        if issuer is None:
            issuer = VoiceControlBindingIssuer.from_environ()
            self._voice_binding_issuer = issuer
        return issuer

    @staticmethod
    def _voice_credential_expiry(claims: Dict[str, Any]) -> datetime:
        """Return the real Keycloak expiry, with a development-only mock seam."""

        expiry = claims.get("exp")
        if isinstance(expiry, (int, float)) and not isinstance(expiry, bool):
            try:
                return datetime.fromtimestamp(expiry, tz=UTC)
            except (OSError, OverflowError, ValueError):
                pass
        environment = os.getenv("ASTRAL_ENV", "").strip().lower() or "production"
        if environment in {"development", "dev", "test"}:
            return datetime.now(UTC) + timedelta(minutes=10)
        raise VoiceControlBindingError("credential_expiry_unavailable")

    async def _issue_voice_control_binding(
        self,
        websocket,
        registration: RegisterUI,
        claims: Dict[str, Any],
    ) -> bool:
        """Deliver one memory-only bearer after authenticated registration."""

        if registration.device_id is None:
            return False
        if registration.connection_generation is None:
            raise VoiceControlBindingError("connection_generation_required")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise VoiceControlBindingError("binding_subject_unavailable")
        prior = self._voice_control_bindings.get(id(websocket))
        if prior is not None and (
            prior.device_id != registration.device_id
            or prior.connection_generation != registration.connection_generation
        ):
            raise VoiceControlBindingError("binding_scope_mismatch")
        issued = self._voice_binding_runtime_issuer().mint(
            subject=subject,
            device_id=registration.device_id,
            connection_generation=registration.connection_generation,
            credential_expires_at=self._voice_credential_expiry(claims),
        )
        frame = VoiceControlBinding(
            device_id=issued.claims.device_id,
            connection_generation=issued.claims.connection_generation,
            binding_id=issued.claims.binding_id,
            binding=issued.bearer,
            expires_at=(
                issued.claims.expires_at.isoformat().replace("+00:00", "Z")
            ),
        )
        socket_id = id(websocket)
        device_key = (issued.claims.subject, issued.claims.device_id)
        device_bindings = getattr(self, "_voice_device_bindings", None)
        if device_bindings is None:
            device_bindings = {}
            self._voice_device_bindings = device_bindings
        composer_tasks = getattr(self, "_voice_composer_tasks", None)
        if composer_tasks is None:
            composer_tasks = {}
            self._voice_composer_tasks = composer_tasks
        composer_revisions = getattr(self, "_voice_composer_revisions", None)
        if composer_revisions is None:
            composer_revisions = {}
            self._voice_composer_revisions = composer_revisions
        device_kinds = getattr(self, "_voice_device_kinds", None)
        if device_kinds is None:
            device_kinds = {}
            self._voice_device_kinds = device_kinds
        displaced_socket_id = device_bindings.get(device_key)
        prior_device_kind = device_kinds.get(device_key)

        # Install the binding before delivering it. WebSocket delivery and the
        # client's first REST mutation run on independent connections, so a
        # client can legitimately present the bearer as soon as ``send``
        # completes. Roll back exactly to the prior socket/device state when
        # delivery fails; no bearer that the client did not receive remains
        # current.
        self._voice_control_bindings[socket_id] = issued.claims
        device_bindings[device_key] = socket_id
        device_kinds[device_key] = self._voice_device_kind(registration)
        if not await self._safe_send(websocket, frame.to_json()):
            if prior is None:
                self._voice_control_bindings.pop(socket_id, None)
            else:
                self._voice_control_bindings[socket_id] = prior
            if displaced_socket_id is None:
                device_bindings.pop(device_key, None)
            else:
                device_bindings[device_key] = displaced_socket_id
            if prior_device_kind is None:
                device_kinds.pop(device_key, None)
            else:
                device_kinds[device_key] = prior_device_kind
            return False
        if displaced_socket_id is not None and displaced_socket_id != socket_id:
            self._voice_control_bindings.pop(displaced_socket_id, None)
            displaced_task = composer_tasks.pop(
                displaced_socket_id, None
            )
            if displaced_task is not None:
                displaced_task.cancel()
            composer_revisions.pop(displaced_socket_id, None)
        return True

    @staticmethod
    def _voice_device_kind(registration: RegisterUI) -> str:
        value = (registration.device or {}).get("device_type")
        if value == "watch":
            return "watchos"
        if value in {"windows", "android", "ios", "macos", "watchos"}:
            return value
        return "web"

    def _clear_voice_control_binding(self, websocket) -> None:
        """Fence the socket's binding synchronously before auth teardown."""

        socket_id = id(websocket)
        # A few deliberately minimal runtime harnesses construct an
        # ``Orchestrator`` with ``__new__`` so they can exercise the connection
        # pumps without booting the voice subsystem.  Disconnect cleanup must
        # remain safe for those pre-voice and partially constructed instances;
        # a fully initialized runtime still owns the real mapping.
        claims = getattr(self, "_voice_control_bindings", {}).pop(socket_id, None)
        composer_tasks = getattr(self, "_voice_composer_tasks", {})
        task = composer_tasks.pop(socket_id, None)
        if task is not None:
            task.cancel()
        getattr(self, "_voice_composer_revisions", {}).pop(socket_id, None)
        if claims is None:
            return
        device_bindings = getattr(self, "_voice_device_bindings", {})
        device_key = (claims.subject, claims.device_id)
        if device_bindings.get(device_key) == socket_id:
            device_bindings.pop(device_key, None)
            getattr(self, "_voice_device_kinds", {}).pop(device_key, None)

    async def publish_voice_composer_state(
        self,
        *,
        user_id: str,
        device_id: str,
        connection_generation: str,
        selected_chat_id: str | None = None,
    ) -> Dict[str, Any] | None:
        """Push one current, owner-validated composer projection."""

        socket_id = self._voice_device_bindings.get((user_id, device_id))
        if socket_id is None:
            return None
        claims = self._voice_control_bindings.get(socket_id)
        if claims is None or claims.connection_generation != connection_generation:
            return None
        websocket = next(
            (candidate for candidate in self.ui_sessions if id(candidate) == socket_id),
            None,
        )
        runtime = getattr(self, "voice_runtime", None)
        if websocket is None or runtime is None:
            return None
        revision = self._voice_composer_revisions.get(socket_id, -1) + 1
        if selected_chat_id is None:
            selected_chat_id = self._ws_active_chat.get(socket_id)
        try:
            frame = await runtime.get_composer_state(
                user_id=user_id,
                device_id=device_id,
                device_kind=self._voice_device_kinds.get(
                    (user_id, device_id), "web"
                ),
                connection_generation=connection_generation,
                selected_chat_id=selected_chat_id,
                revision=revision,
            )
        except Exception as exc:
            logger.warning(
                "voice_composer_projection_failed",
                extra={"reason": getattr(exc, "code", type(exc).__name__)},
            )
            return None
        if not await self._safe_send(websocket, json.dumps(frame)):
            return None
        self._voice_composer_revisions[socket_id] = revision
        return dict(frame)

    def _start_voice_composer_refresh(
        self,
        *,
        user_id: str,
        device_id: str,
        connection_generation: str,
        selected_chat_id: str | None,
    ) -> None:
        socket_id = self._voice_device_bindings.get((user_id, device_id))
        if socket_id is None:
            return
        prior = self._voice_composer_tasks.pop(socket_id, None)
        if prior is not None:
            prior.cancel()

        async def _refresh() -> None:
            for _attempt in range(46):
                frame = await self.publish_voice_composer_state(
                    user_id=user_id,
                    device_id=device_id,
                    connection_generation=connection_generation,
                    selected_chat_id=selected_chat_id,
                )
                if frame is None or frame.get("voice", {}).get("available") is True:
                    return
                await asyncio.sleep(2)

        task = asyncio.create_task(_refresh())
        self._voice_composer_tasks[socket_id] = task

        def _forget(completed: asyncio.Task[Any]) -> None:
            if self._voice_composer_tasks.get(socket_id) is completed:
                self._voice_composer_tasks.pop(socket_id, None)

        task.add_done_callback(_forget)

    def validate_voice_control_binding(
        self,
        *,
        bearer: str,
        subject: str,
        device_id: str,
        connection_generation: str,
    ) -> VoiceControlClaims:
        """Verify a REST control bearer against the currently registered socket.

        A valid HMAC is deliberately insufficient: reconnect, reauthentication,
        binding rotation, or socket teardown removes the matching claims from
        ``_voice_control_bindings`` and immediately fences the old bearer.
        """

        claims = self._voice_binding_runtime_issuer().verify(
            bearer,
            expected_subject=subject,
            expected_device_id=device_id,
            expected_connection_generation=connection_generation,
        )
        device_bindings = getattr(self, "_voice_device_bindings", None)
        if device_bindings is None:
            current = any(
                value == claims for value in self._voice_control_bindings.values()
            )
        else:
            socket_id = device_bindings.get((claims.subject, claims.device_id))
            current = (
                socket_id is not None
                and self._voice_control_bindings.get(socket_id) == claims
            )
        if not current:
            raise VoiceControlBindingError("binding_not_current")
        return claims

    async def _safe_send(self, websocket, data: str) -> bool:
        """Send data over a websocket, returning False if the connection is closed."""
        try:
            data = self._scope_conversation_transient(websocket, data)
            if hasattr(websocket, "send_text"):
                # FastAPI WebSocket
                await websocket.send_text(data)
            else:
                # websockets library WebSocket
                await websocket.send(data)
            self._trace_frame(websocket, data, ok=True)
            return True
        except Exception as e:
            logger.debug(f"Failed to send message (connection likely closed): {e}")
            self._trace_frame(websocket, data, ok=False, error=repr(e))
            return False

    def _trace_frame(self, websocket, data: str, *, ok: bool, error: str = "") -> None:
        """Diagnostic outbound-frame trace, enabled by a marker file so a
        running container can flip it without an env-recreate. Fail-open.

        The trace is metadata-only. Outbound frames routinely contain chat
        text, recap text, PHI, provider material, or short-lived voice
        capabilities, so even an explicitly armed diagnostic trace must never
        become a second content-retention channel (065 FR-046/FR-047).
        """
        try:
            if not os.path.exists("/app/.frame_trace"):
                return
            ftype = "?"
            trace_data = "[REDACTED]"
            try:
                trace_frame = json.loads(data)
            except (json.JSONDecodeError, TypeError, ValueError):
                trace_frame = None
            if trace_frame is not None:
                candidate_type = (
                    trace_frame.get("type") if isinstance(trace_frame, dict) else None
                )
                if isinstance(candidate_type, str) and re.fullmatch(
                    r"[a-z][a-z0-9_]{0,127}", candidate_type
                ):
                    ftype = candidate_type
                trace_data = json.dumps(
                    {"type": ftype, "frame": "[REDACTED]"},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            error = "redacted_send_failure" if error else ""
            sock = type(websocket).__name__
            logger.info(
                "FRAME_TRACE type=%s sock=%s id=%s bytes=%d ok=%s %s",
                ftype,
                sock,
                id(websocket),
                len(data),
                ok,
                error,
            )
            with open("/app/frame_trace.jsonl", "a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "ts": time.time(),
                            "type": ftype,
                            "sock": sock,
                            "sock_id": id(websocket),
                            "ok": ok,
                            "error": error,
                            "frame": trace_data,
                        }
                    )
                    + "\n"
                )
        except Exception:
            pass


    def _vws_fan_targets(self, websocket) -> List[Any]:
        """Real sockets that must mirror a VirtualWebSocket-bound chat frame
        (055 bg-continuity): the task-owner's sockets active on the task's
        chat. Empty for real sockets (no double delivery), when
        FF_BG_CONTINUITY is off, or when the background task carries no
        user+chat identity (e.g. bare handshake/test tasks)."""
        from orchestrator.async_tasks import VirtualWebSocket
        if not isinstance(websocket, VirtualWebSocket):
            return []
        if not flags.is_enabled("bg_continuity"):
            return []
        task = getattr(websocket, "task", None)
        user_id = getattr(task, "user_id", None)
        chat_id = getattr(task, "chat_id", None)
        if not user_id or not chat_id:
            return []
        # The executing socket never fans to itself (a registered
        # VirtualWebSocket would otherwise recurse).
        return [ws for ws in self._sockets_on_chat(user_id, chat_id)
                if ws is not websocket]

    async def _send_chat_status(self, websocket, status: str, message: str = ""):
        """Send a chat_status frame; a VirtualWebSocket-bound frame also fans
        to the user's real sockets on the task's chat (055 bg-continuity) so
        background turns surface their terminal state on every device."""
        data = json.dumps({"type": "chat_status", "status": status, "message": message})
        await self._safe_send(websocket, data)
        for ws in self._vws_fan_targets(websocket):
            try:
                await self._safe_send(ws, data)
            except Exception:  # pragma: no cover - per-socket best-effort
                logger.debug("vws chat_status fan failed", exc_info=True)

    async def _broadcast_user_history(self, user_id: str | None = None):
        """Send each connected UI client their own user's recent chat history.

        Groups clients by user_id to avoid redundant DB queries when the
        same user has multiple tabs open.  ``user_id`` narrows commit-driven
        refreshes to the owner whose durable conversation projection changed;
        callers that omit it retain the existing all-user refresh behavior.
        """
        if not self.ui_clients:
            return

        clients_by_user: Dict[str, list] = {}
        for client in self.ui_clients:
            uid = self._get_user_id(client)
            if user_id is not None and uid != user_id:
                continue
            clients_by_user.setdefault(uid, []).append(client)

        tasks = []
        for uid, clients in clients_by_user.items():
            history_list = await asyncio.to_thread(
                self.history.get_recent_chats, user_id=uid)
            msg = json.dumps({"type": "history_list", "chats": history_list})
            for c in clients:
                tasks.append(self._safe_send(c, msg))
                # Feature 037: refresh the server-driven, ROTE-adapted surface.
                tasks.append(self._push_history_surface(c, chats=history_list))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _refresh_history_after_commit(self, user_id: str) -> None:
        """Fail-soft owner-scoped history projection after a durable commit."""

        try:
            await self._broadcast_user_history(user_id=user_id)
        except Exception:
            # The conversation commit and its snapshot are authoritative.
            # A transient history-query/render failure recovers on the next
            # commit, explicit history request, or socket registration.
            logger.warning(
                "committed conversation history refresh failed",
                exc_info=True,
            )

    async def _push_history_surface(self, websocket, *, chats=None, loading: bool = False) -> None:
        """Feature 037: render the chat-history surface (skeleton while loading,
        else the recent-chats list) and push it ROTE-adapted to the client's
        history region via send_ui_render(target="history"). Fail-soft — a
        surface error never breaks history delivery."""
        try:
            from orchestrator.history_surface import (
                history_skeleton_components,
                history_surface_components,
            )
            comps = (history_skeleton_components() if loading
                     else history_surface_components(chats or []))
            await self.send_ui_render(websocket, comps, target="history")
        except Exception:
            logger.debug("history surface push failed", exc_info=True)

    @staticmethod
    def _derive_chat_title(content: str, default: str = "Response") -> str:
        """Feature 029 (FR-027): a contextual chat-card title.

        Prefers the response's own first markdown heading; falls back to the
        provided default. Never invents content — purely derivational.
        """
        for line in (content or "").splitlines():
            line = line.strip()
            if line.startswith("#"):
                heading = line.lstrip("#").strip().rstrip(":")
                if heading:
                    return heading[:80]
        return default

    def _chat_narrative(self, content: str, chat_id: Optional[str] = None) -> List[Dict]:
        """Feature 029 (FR-027): the final-turn chat-panel narrative.

        Replaces the constant ``Card(title="Analysis")``: short plain answers
        render as bare markdown (no card chrome); longer ones get a card with
        a title derived from the response itself.
        """
        text = _strip_toolcall_leakage(content)
        if not text and (content or "").strip():
            _log_stripped_empty("chat_narrative", chat_id, content)
            text = _LEAK_FALLBACK_TEXT
        if len(text) <= 280 and "\n\n" not in text and not text.startswith("#"):
            return [Text(content=text, variant="markdown").to_dict()]
        return [
            Card(title=self._derive_chat_title(text), content=[
                Text(content=text, variant="markdown")
            ]).to_dict()
        ]

    def _canvas_components(self, chat_id: str, user_id: str) -> List[Dict]:
        """Feature 029: the canvas as one component list — designed arrangements
        materialized in place, unclaimed components flat — in shared position
        order. With no arrangements this is exactly the pre-029 flat canvas."""
        layouts = self.workspace.live_layouts(chat_id, user_id)
        if not layouts:
            components = self.workspace.live_components(chat_id, user_id)
            _stamp_canvas_provenance(components)
            return components
        from orchestrator import ui_designer
        from orchestrator.workspace import iter_layout_refs
        by_id: Dict[str, Dict] = {}
        comp_entries = []
        for row in self.workspace.live_rows(chat_id, user_id):
            data = row.get("component_data")
            if not isinstance(data, dict):
                continue
            cid = row.get("component_id")
            if cid and not data.get("component_id"):
                data["component_id"] = cid
            if cid:
                by_id[cid] = data
            comp_entries.append((row.get("position") or 0, cid, data))
        claimed = set()
        for lay in layouts:
            claimed |= set(iter_layout_refs(lay["layout"]))
        stream: List = [
            (pos, 0, [data]) for pos, cid, data in comp_entries
            if not (cid and cid in claimed)
        ]
        stream += [
            (lay.get("position") or 0, 1, ui_designer.materialize(lay["layout"], by_id))
            for lay in layouts
        ]
        out: List[Dict] = []
        for _pos, _kind, payload in sorted(stream, key=lambda t: (t[0], t[1])):
            out.extend(payload)
        _stamp_canvas_provenance(out)
        return out

    async def _push_canvas(self, chat_id: str, user_id: str, originating_ws=None,
                           turn_marker: Optional[str] = None):
        """Full-canvas ui_render (materialized arrangements) to every socket of
        the user on this chat — the same fan-out + per-socket ROTE adaptation
        the legacy reconciliation path uses.

        ``turn_marker`` (055 US3): the chat's latest message id when the canvas
        was designed — the push is dropped when the chat has since moved on
        (cross-socket/async stale guard)."""
        if turn_marker is not None:
            latest = str(await asyncio.to_thread(
                self.history.get_latest_message_id, chat_id, user_id=user_id) or "")
            if latest != str(turn_marker):
                logger.info("ui_designer: chat %s advanced past the designed "
                            "turn — canvas push dropped", chat_id)
                return
        components = await asyncio.to_thread(self._canvas_components, chat_id, user_id)
        targets = [
            ws for ws in self.ui_clients
            if self._get_user_id(ws) == user_id
            and self._ws_active_chat.get(id(ws)) == chat_id
        ]
        if originating_ws is not None and originating_ws not in targets:
            targets.append(originating_ws)
        for ws in targets:
            # speak=False: a full-canvas push re-presents existing content —
            # watch sockets must never re-speak it (055 US3 / FR-030).
            await self.send_ui_render(ws, components, speak=False)

    async def _run_designer(self, websocket, components: List[Dict], chat_id: str,
                            user_id: str, user_request: str, layout_key: str,
                            *, progress: bool = True) -> Optional[List[Dict]]:
        """One bounded designer conversation over ``components``; returns the
        validated arrangement or ``None`` (None ALWAYS = keep the flat canvas).

        ``progress=False`` suppresses the per-pass ``chat_status`` frames —
        required on the post-done native pass (055 US3), where they would flip
        clients back to turn-active behind a stuck status line."""
        from orchestrator import ui_designer

        _designer_pass = {"n": 0}

        async def _designer_llm(messages):
            # Same credential resolution as the round itself (feature 006,
            # websocket-scoped) and the same llm_call auditing (FR-028).
            # Feature 030: each pass announces itself — the walkthrough
            # measured an 83 s frame-silent gap while the designer worked
            # behind a stale status line.
            _designer_pass["n"] += 1
            if progress:
                try:
                    await self._safe_send(websocket, json.dumps({
                        "type": "chat_status", "status": "thinking",
                        "message": f"Designing your layout (pass {_designer_pass['n']} of "
                                   f"{ui_designer.designer_max_rounds()})...",
                    }))
                except Exception:
                    logger.debug("designer progress status send failed", exc_info=True)
            msg, _usage = await self._call_llm(
                websocket, messages, tools_desc=None,
                temperature=0.2, feature="ui_designer",
            )
            return (msg.content or "") if msg else None

        from webrender import allowed_primitive_types
        # The current persisted arrangement for this layout_key (if any)
        # lets the designer avoid re-arranging it for a marginal gain.
        _current_layout = None
        try:
            for _lay in await asyncio.to_thread(
                    self.workspace.live_layouts, chat_id, user_id):
                if _lay.get("layout_key") == layout_key:
                    _current_layout = _lay.get("layout")
                    break
        except Exception:
            _current_layout = None
        canvas_rows = await asyncio.to_thread(
            self.workspace.live_rows, chat_id, user_id)
        with perf_span("turn.designer", chat=chat_id):
            return await ui_designer.design_round(
                user_request=user_request,
                round_components=components,
                canvas_rows=canvas_rows,
                chat_id=chat_id,
                layout_key=layout_key,
                allowed_types=set(allowed_primitive_types()),
                llm_call=_designer_llm,
                current_layout=_current_layout,
            )

    async def _deliver_round_components(self, websocket, components: List[Dict], chat_id: str,
                                        user_id: str, *, user_request: str = "") -> List[Dict]:
        """Feature 029: deliver one round's rich components to the canvas.

        Feature 052 (FR-013) delivery order — upsert FIRST: components persist
        (identities assigned by the unchanged 028 upsert) and the flat
        ``ui_upsert`` goes out immediately, exactly like the native branch;
        THEN rounds with ≥2 components (flag-gated) get the adaptive designer
        pass, whose designed canvas lands as a later in-place refinement
        (morph anchors preserve identity). ANY designer failure simply means
        the refinement never arrives — the already-delivered flat components
        are the legacy fallback rendering, no user-visible error (FR-022).
        """
        from orchestrator import ui_designer
        from orchestrator.workspace import layout_key_for
        timeline = self._ws_timeline_mode.get(id(websocket), False)
        # Native clients render structured components with their OWN responsive
        # layout, so the per-round designer pass never runs for them: rounds
        # deliver flat, and with FF_DESIGNER_ALL_DEVICES on the turn handler
        # runs ONE coalesced post-done pass instead (_design_turn_post_done,
        # 055 US3). Flag off restores the 052 skip (native-origin turns are
        # never designed — the walkthrough saw a ~150 s mid-turn wait).
        _prof = self.rote.get_profile(websocket) if websocket is not None else None
        if _is_native_device(_prof):
            return await self._send_or_replace_components(websocket, components, chat_id, user_id)
        if not chat_id or not ui_designer.should_design(components, timeline_mode=timeline):
            return await self._send_or_replace_components(websocket, components, chat_id, user_id)
        try:
            ops = await asyncio.to_thread(self.workspace.upsert, chat_id, user_id, components)
        except Exception:
            logger.exception("workspace upsert failed — falling back to transient render")
            await self.send_ui_render(websocket, components)
            return []
        # Feature 052 (FR-013): upsert-first — the flat components reach the
        # user immediately (exactly the native-branch delivery); the designed
        # arrangement below arrives as a later in-place refinement.
        await self.send_ui_upsert(websocket, chat_id, user_id, ops)
        layout = None
        try:
            turn_marker = str(await asyncio.to_thread(
                self.history.get_latest_message_id, chat_id, user_id=user_id) or "")
            layout_key = layout_key_for(chat_id, turn_marker)
            layout = await self._run_designer(
                websocket, components, chat_id, user_id, user_request, layout_key)
        except Exception:
            logger.exception("ui_designer crashed — flat ui_upsert already delivered")
            layout = None
        if layout:
            try:
                await asyncio.to_thread(
                    self.workspace.upsert_layout, chat_id, user_id, layout_key, layout)
                # Stale-chat guard (FR-013): the designed refinement is only
                # forced to the originating socket while it still views this
                # chat; other sockets are chat-filtered inside _push_canvas.
                if self._ws_active_chat.get(id(websocket)) == chat_id:
                    await self._push_canvas(chat_id, user_id, originating_ws=websocket,
                                            turn_marker=turn_marker)
                else:
                    logger.info(
                        "ui_designer: originating socket left chat %s — "
                        "designed render not forced to it", chat_id)
                    await self._push_canvas(chat_id, user_id, originating_ws=None,
                                            turn_marker=turn_marker)
            except Exception:
                logger.exception(
                    "designed canvas delivery failed — flat ui_upsert already delivered")
        # Audit the mutation (FR-023) — identical to the flat path.
        try:
            from audit.hooks import record_workspace_event
            for op in ops:
                asyncio.create_task(record_workspace_event(
                    user_id=user_id,
                    action="component_updated" if not op.get("created") else "component_added",
                    chat_id=chat_id, component_id=op.get("component_id"),
                ))
        except Exception:
            logger.debug("workspace audit failed", exc_info=True)
        return ops

    async def _design_turn_post_done(
        self,
        websocket,
        chat_id: str,
        user_id: str,
        user_request: str,
        turn_components: List[Dict],
        *,
        turn_marker: Any = None,
    ) -> Optional[str]:
        """Prepare 055's one coalesced native design for post-done delivery.

        Feature 060 requires the layout to be frozen into the same atomic
        transcript/canvas publication, so design and persistence happen before
        that publication.  The caller emits the materialized refinement only
        after ``chat_status done`` and before returning (therefore still before
        async ``task_completed``).  Designer progress remains suppressed.

        Returns the exact final message marker when a layout was persisted;
        ``None`` keeps the already-delivered flat components authoritative.
        """
        from orchestrator.scheduled_publication import (
            current_scheduled_history_stage,
        )

        from orchestrator.conversation_publication import (
            current_conversation_publication,
        )

        if (
            current_scheduled_history_stage() is not None
            and current_conversation_publication() is None
        ):
            return None
        if not chat_id or not turn_components:
            return None
        if not flags.is_enabled("designer_all_devices"):
            return None
        prof = self.rote.get_profile(websocket) if websocket is not None else None
        if not _is_native_device(prof):
            return None
        from orchestrator import ui_designer
        from orchestrator.workspace import layout_key_for
        timeline = self._ws_timeline_mode.get(id(websocket), False)
        components = _native_canvas_components(turn_components)
        if not ui_designer.should_design(components, timeline_mode=timeline):
            return None
        try:
            marker = str(
                turn_marker
                or await asyncio.to_thread(
                    self.history.get_latest_message_id,
                    chat_id,
                    user_id=user_id,
                )
                or ""
            )
            layout_key = layout_key_for(chat_id, marker)
            layout = await self._run_designer(
                websocket, components, chat_id, user_id, user_request,
                layout_key, progress=False)
            if not layout:
                return None
            await asyncio.to_thread(
                self.workspace.upsert_layout, chat_id, user_id, layout_key, layout)
            return marker
        except Exception:
            logger.exception(
                "ui_designer.post_done_failed chat=%s user=%s — flat components "
                "already delivered", chat_id, user_id)
            return None

    async def _push_designed_native_canvas(self, chat_id: str, user_id: str,
                                           originating_ws, turn_marker: str) -> None:
        """Out-of-turn full ``ui_render`` of the designed canvas (055 US3):
        materialized pre-ROTE, doc/Reasoning-filtered for native sockets,
        never spoken, dropped when a newer turn has started on the chat."""
        latest = str(await asyncio.to_thread(
            self.history.get_latest_message_id, chat_id, user_id=user_id) or "")
        if latest != str(turn_marker):
            logger.info("ui_designer.post_done_dropped chat=%s user=%s turn=%s "
                        "latest=%s reason=stale_turn",
                        chat_id, user_id, turn_marker, latest)
            return
        components = await asyncio.to_thread(self._canvas_components, chat_id, user_id)
        native_components = _native_canvas_components(components)
        targets = [
            ws for ws in self.ui_clients
            if self._get_user_id(ws) == user_id
            and self._ws_active_chat.get(id(ws)) == chat_id
        ]
        if originating_ws is not None and originating_ws not in targets:
            targets.append(originating_ws)
        for ws in targets:
            payload = (native_components
                       if _is_native_device(self.rote.get_profile(ws))
                       else components)
            await self.send_ui_render(ws, payload, speak=False)

    async def _send_or_replace_components(self, websocket, components: List[Dict], chat_id: str,
                                          user_id: str, *, force_component_id: Optional[str] = None) -> List[Dict]:
        """Feature 028: persist rich components into the chat's workspace under
        stable identities and push partial ``ui_upsert`` updates (research D12).

        Replaces the pre-028 ``(tool, agent)`` matcher whose
        ``components_replaced`` messages the thin client silently dropped —
        the disappearing-UI defect. Updates morph in place on every socket of
        this user viewing the chat (FR-040); new components append. Returns
        the persisted op list so callers can snapshot the turn (FR-030).

        055 US4: stamps ``provenance`` on every component before it persists
        or renders — this is where model-authored components (parsed rounds,
        narrative doc cards) enter the workspace without passing
        ``_tag_source``, so a model-supplied trust value is overwritten here.
        """
        if not components:
            return []
        for comp in components:
            _stamp_provenance(comp)
        from orchestrator.scheduled_publication import (
            current_scheduled_history_stage,
        )

        if current_scheduled_history_stage() is not None:
            # Scheduled output is captured by VirtualWebSocket and persisted
            # only through the atomic staged-history publication.  A workspace
            # upsert here would become visible before the effect ledger commit.
            await self.send_ui_render(websocket, components)
            return []
        if not chat_id:
            await self.send_ui_render(websocket, components)
            return []
        try:
            ops = await asyncio.to_thread(
                self.workspace.upsert, chat_id, user_id, components,
                force_component_id=force_component_id)
        except Exception:
            logger.exception("workspace upsert failed — falling back to transient render")
            await self.send_ui_render(websocket, components)
            return []
        await self.send_ui_upsert(websocket, chat_id, user_id, ops)
        # Audit the mutation (FR-023) without blocking the turn.
        try:
            from audit.hooks import record_workspace_event
            for op in ops:
                asyncio.create_task(record_workspace_event(
                    user_id=user_id,
                    action="component_updated" if not op.get("created") else "component_added",
                    chat_id=chat_id, component_id=op.get("component_id"),
                ))
        except Exception:
            logger.debug("workspace audit failed", exc_info=True)
        return ops

    def _component_action_allowed(self, user_id: str, agent_id: str, tool_name: str):
        """FR-036: deterministic component actions pass the SAME gates as the
        chat path — security-flag blocks and per-user tool permissions
        (the pre-028 ``table_paginate`` skipped both)."""
        agent_flags = self.security_flags.get(agent_id, {}) if hasattr(self, "security_flags") else {}
        flag = agent_flags.get(tool_name)
        if flag and flag.get("blocked"):
            return False, "This tool is blocked by a security review."
        try:
            if not self.tool_permissions.is_tool_allowed(user_id, agent_id, tool_name):
                return False, "This tool is disabled in your permissions."
        except Exception:
            logger.exception("component_action: permission check failed — denying")
            return False, "Permission check failed."
        return True, ""

    async def _handle_component_action(self, websocket, user_id: str, payload: Dict[str, Any]):
        """Feature 028 — standardized deterministic component action
        (contracts/component-action.md): resolve the emitting component's
        provenance, re-check permissions, re-execute its source capability,
        and upsert the result into the target component in place."""
        chat_id = payload.get("chat_id") or self._ws_active_chat.get(id(websocket))
        component_id = payload.get("component_id")
        target_id = payload.get("target_component_id") or component_id
        params_patch = payload.get("params_patch") or {}
        # Contract (component-action.md): 'refresh' and 'invoke' are the
        # deterministic kinds — both re-execute the source capability with the
        # patched params. Anything else is refused explicitly (intent actions
        # never arrive on this verb; they use the param_picker chat idiom).
        kind = str(payload.get("kind") or "refresh").lower()
        if kind not in ("refresh", "invoke"):
            await self._audit_workspace_denial(user_id, chat_id or "", component_id or "",
                                               f"unsupported_kind:{kind}")
            await self.send_ui_render(websocket, [
                Alert(message=f"Unsupported component action kind '{kind}'.",
                      variant="error").to_dict()
            ], target="chat")
            return
        if not chat_id or not component_id:
            await self.send_ui_render(websocket, [
                Alert(message="This action is missing its component context.", variant="error").to_dict()
            ], target="chat")
            return
        # Timeline guard (FR-031): historical views are strictly read-only.
        if self._ws_timeline_mode.get(id(websocket)):
            await self._audit_workspace_denial(user_id, chat_id, component_id, "timeline_readonly")
            await self.send_ui_render(websocket, [
                Alert(message="You are viewing a past workspace state — return to live to interact.",
                      variant="warning").to_dict()
            ], target="chat")
            return
        row = await self.workspace.aget_by_component_id(chat_id, user_id, component_id)
        if row is None or not isinstance(row.get("component_data"), dict):
            await self.send_ui_render(websocket, [
                Alert(message="This component is no longer available.", variant="warning").to_dict()
            ], target="chat")
            return
        cd = row["component_data"]
        agent_id = cd.get("_source_agent", "")
        tool_name = cd.get("_source_tool", "")
        if not agent_id or not tool_name:
            await self.send_ui_render(websocket, [
                Alert(message="This component has no refreshable source.", variant="warning").to_dict()
            ], target="chat")
            return
        # Feature 029 (FR-004): retired sources get a clear retirement message
        # (audited), merged sources transparently reroute to ml-services-1.
        if agent_id in RETIRED_AGENT_IDS:
            await self._audit_workspace_denial(user_id, chat_id, component_id, "agent_retired")
            await self.send_ui_render(websocket, [
                Alert(
                    title="Capability retired",
                    message="This component came from an agent that has been retired; "
                            "it can still be viewed but no longer refreshed.",
                    variant="warning",
                ).to_dict()
            ], target="chat")
            return
        agent_id, tool_name = remap_merged_source(agent_id, tool_name)
        allowed, deny_reason = await asyncio.to_thread(
            self._component_action_allowed, user_id, agent_id, tool_name)
        if not allowed:
            await self._audit_workspace_denial(user_id, chat_id, component_id, deny_reason)
            await self.send_ui_render(websocket, [
                Alert(message=f"Action not permitted: {deny_reason}", variant="error").to_dict()
            ], target="chat")
            return

        params = dict(cd.get("_source_params") or {})
        if isinstance(params_patch, dict):
            params.update(params_patch)

        try:
            async with Orchestrator._workspace_mutation_lock(self, chat_id):
                # Deterministic ordering per chat (contract §Concurrency).
                # FR-036: a deterministic component re-execution must face the
                # SAME gate stack as a chat dispatch of the same tool — the
                # policy engine, taint gate, supervisor/HITL, PRE_TOOL_USE hook,
                # RFC 8693 delegation mint, per-user credential + LLM-credential
                # injection, and the concurrency cap — not merely the
                # security-flag + permission pre-check above (kept as a fast
                # fail). Without this, clicking Refresh/invoke on a saved
                # component ran a policy-denied or HITL-confirm-required tool
                # that the chat path would have blocked.
                auth = await self._authorize_and_prepare(
                    websocket, agent_id, tool_name, dict(params), chat_id, user_id)
                if isinstance(auth, GateRefusal):
                    await self._audit_workspace_denial(
                        user_id, chat_id, component_id,
                        (str((auth.response.error or {}).get("message"))[:200]
                         if auth.response and auth.response.error else "gate_refused"))
                    if auth.render_components:
                        await self.send_ui_render(
                            websocket, auth.render_components,
                            target=auth.render_target or "chat")
                    return
                result = await self._execute_with_retry(websocket, agent_id, tool_name, auth.args)
                if result and result.ui_components and not result.error:
                    for comp in result.ui_components:
                        if isinstance(comp, dict):
                            comp["_source_agent"] = agent_id
                            comp["_source_tool"] = tool_name
                            comp["_source_params"] = params
                    ops = await self._send_or_replace_components(
                        websocket, result.ui_components, chat_id, user_id=user_id,
                        force_component_id=target_id,
                    )
                    if ops:
                        try:
                            await self.workspace.asnapshot(chat_id, user_id, cause="component_action")
                        except Exception:
                            logger.debug("workspace snapshot failed (component_action)", exc_info=True)
                elif result and result.error:
                    await self.send_ui_render(websocket, [
                        Alert(message=result.error.get("message", "The action failed."),
                              variant="error").to_dict()
                    ], target="chat")
        except Exception as e:
            logger.error(f"component_action failed: {e}", exc_info=True)
            await self.send_ui_render(websocket, [
                Alert(message=f"The action failed: {e}", variant="error").to_dict()
            ], target="chat")
        finally:
            await self._safe_send(websocket, json.dumps({
                "type": "chat_status", "status": "done", "message": ""
            }))

    async def _refine_restore_gate(self, websocket, user_id: str,
                                   payload: Dict[str, Any]):
        """055 US4 (wire-contract §3): shared gate sequence for
        component_refine/component_restore — the component_action stack:
        feature flag, watch carve-out, component context, timeline read-only
        guard, row existence, retired-source refusal, security flags +
        per-user permission on the source agent/tool. The source gates are
        skipped only when the component has no source at all (a
        model-authored artifact has no tool to gate; the refine LLM gate
        still applies in the caller). Refusals are per-action error frames
        (Alert to the chat rail), never socket teardown.

        Returns ``(chat_id, component_id, row)`` or ``None`` after refusing.
        """
        chat_id = payload.get("chat_id") or self._ws_active_chat.get(id(websocket))
        component_id = payload.get("component_id")
        if not flags.is_enabled("component_refine"):
            await self._audit_workspace_denial(user_id, chat_id or "", component_id or "",
                                               "feature_disabled")
            await self.send_ui_render(websocket, [
                Alert(message="Component refine is not enabled on this server.",
                      variant="error").to_dict()
            ], target="chat")
            return None
        # Declared ROTE-capability divergence: the watch renders no
        # refine/restore affordance and the server refuses honestly.
        profile = self.rote.get_profile(websocket)
        if getattr(profile.device_type, "value", str(profile.device_type)) == "watch":
            await self._audit_workspace_denial(user_id, chat_id or "", component_id or "",
                                               "watch_unsupported")
            await self.send_ui_render(websocket, [
                Alert(message="This action isn't available on the watch.",
                      variant="warning").to_dict()
            ], target="chat")
            return None
        if not chat_id or not component_id:
            await self.send_ui_render(websocket, [
                Alert(message="This action is missing its component context.",
                      variant="error").to_dict()
            ], target="chat")
            return None
        if self._ws_timeline_mode.get(id(websocket)):
            await self._audit_workspace_denial(user_id, chat_id, component_id, "timeline_readonly")
            await self.send_ui_render(websocket, [
                Alert(message="You are viewing a past workspace state — return to live to interact.",
                      variant="warning").to_dict()
            ], target="chat")
            return None
        row = await self.workspace.aget_by_component_id(chat_id, user_id, component_id)
        if row is None or not isinstance(row.get("component_data"), dict):
            await self.send_ui_render(websocket, [
                Alert(message="This component is no longer available.",
                      variant="warning").to_dict()
            ], target="chat")
            return None
        cd = row["component_data"]
        agent_id = cd.get("_source_agent", "")
        tool_name = cd.get("_source_tool", "")
        if agent_id in RETIRED_AGENT_IDS:
            await self._audit_workspace_denial(user_id, chat_id, component_id, "agent_retired")
            await self.send_ui_render(websocket, [
                Alert(
                    title="Capability retired",
                    message="This component came from an agent that has been retired; "
                            "it can still be viewed but no longer changed.",
                    variant="warning",
                ).to_dict()
            ], target="chat")
            return None
        if agent_id and tool_name:
            agent_id, tool_name = remap_merged_source(agent_id, tool_name)
            allowed, deny_reason = await asyncio.to_thread(
                self._component_action_allowed, user_id, agent_id, tool_name)
            if not allowed:
                await self._audit_workspace_denial(user_id, chat_id, component_id, deny_reason)
                await self.send_ui_render(websocket, [
                    Alert(message=f"Action not permitted: {deny_reason}",
                          variant="error").to_dict()
                ], target="chat")
                return None
        return chat_id, component_id, row

    async def _handle_component_refine(self, websocket, user_id: str, payload: Dict[str, Any]):
        """055 US4 (FR-022/FR-023, research D10): bounded LLM edit of ONE
        component in place under its existing identity.

        Gate order is the component_action stack plus the 054 per-user LLM
        gate — a refine is an LLM turn billed to the user's own provider
        config. The source tool is NOT re-run, so the result re-stamps
        provenance as 'estimated'; the prior dict is archived to
        component_version BEFORE the overwrite (FR-024).
        """
        gate = await self._refine_restore_gate(websocket, user_id, payload)
        if gate is None:
            return
        chat_id, component_id, _row = gate
        instruction = str(payload.get("instruction") or "").strip()
        if not instruction:
            await self.send_ui_render(websocket, [
                Alert(message="Tell me how this component should change.",
                      variant="warning").to_dict()
            ], target="chat")
            return
        if not await self.llm_configured_for(user_id):
            actor_user_id, auth_principal = self._llm_audit_principals(websocket)
            await self._record_llm_unconfigured(
                self.audit_recorder,
                actor_user_id=actor_user_id,
                auth_principal=auth_principal,
                feature="ui_event:component_refine",
            )
            await self.send_ui_render(websocket, [
                Alert(message="Set up your AI provider to use this.",
                      variant="error").to_dict()
            ], target="chat")
            return

        from orchestrator import artifact_versions
        try:
            async with Orchestrator._workspace_mutation_lock(self, chat_id):
                # Re-read inside the lock: the archived "current" must be the
                # dict actually being overwritten, not a pre-lock snapshot.
                row = await self.workspace.aget_by_component_id(chat_id, user_id, component_id)
                if row is None or not isinstance(row.get("component_data"), dict):
                    await self.send_ui_render(websocket, [
                        Alert(message="This component is no longer available.",
                              variant="warning").to_dict()
                    ], target="chat")
                    return
                current = row["component_data"]
                refined = await self._refine_component_llm(
                    websocket, current, instruction[:4000])
                if refined is None:
                    await self.send_ui_render(websocket, [
                        Alert(message="The AI couldn't produce a valid same-type edit "
                                      "for this component, so it was left unchanged.",
                              variant="warning").to_dict()
                    ], target="chat")
                    return
                version_no = await artifact_versions.aarchive(
                    self.history.db, chat_id, user_id, component_id, current, "refine")
                # Hydrate the history affordance (web data-versions popover) —
                # without this the restore path is unreachable from the UI.
                refined["versions"] = await artifact_versions.alist_versions(
                    self.history.db, chat_id, user_id, component_id)
                ops = await self.workspace.aupsert(
                    chat_id, user_id, [refined], force_component_id=component_id)
                await self.send_ui_upsert(websocket, chat_id, user_id, ops)
                try:
                    await self.workspace.asnapshot(chat_id, user_id, cause="component_refine")
                except Exception:
                    logger.debug("workspace snapshot failed (component_refine)", exc_info=True)
                try:
                    from audit.hooks import record_workspace_event
                    await record_workspace_event(
                        user_id=user_id, action="component_refined",
                        chat_id=chat_id, component_id=component_id,
                        detail={"archived_version": version_no},
                    )
                except Exception:
                    logger.debug("workspace audit failed (component_refined)", exc_info=True)
        except Exception as e:
            logger.error(f"component_refine failed: {e}", exc_info=True)
            await self.send_ui_render(websocket, [
                Alert(message=f"The refine failed: {e}", variant="error").to_dict()
            ], target="chat")
        finally:
            await self._safe_send(websocket, json.dumps({
                "type": "chat_status", "status": "done", "message": ""
            }))

    async def _handle_component_restore(self, websocket, user_id: str, payload: Dict[str, Any]):
        """055 US4 (FR-024): restore an archived component_version under the
        same identity — the component_action gate stack minus the LLM gate
        (no model runs). The current dict is archived first, so a restore is
        itself undoable and the version chain stays complete."""
        gate = await self._refine_restore_gate(websocket, user_id, payload)
        if gate is None:
            return
        chat_id, component_id, _row = gate
        from orchestrator import artifact_versions
        version = await artifact_versions.aget_version(
            self.history.db, chat_id, user_id, component_id, payload.get("version_no"))
        if version is None or not isinstance(version.get("component"), dict):
            await self.send_ui_render(websocket, [
                Alert(message="That version is no longer available.",
                      variant="warning").to_dict()
            ], target="chat")
            return
        try:
            async with Orchestrator._workspace_mutation_lock(self, chat_id):
                row = await self.workspace.aget_by_component_id(chat_id, user_id, component_id)
                if row is None or not isinstance(row.get("component_data"), dict):
                    await self.send_ui_render(websocket, [
                        Alert(message="This component is no longer available.",
                              variant="warning").to_dict()
                    ], target="chat")
                    return
                current = row["component_data"]
                restored = dict(version["component"])
                restored["component_id"] = component_id
                archived_no = await artifact_versions.aarchive(
                    self.history.db, chat_id, user_id, component_id, current, "restore")
                restored["versions"] = await artifact_versions.alist_versions(
                    self.history.db, chat_id, user_id, component_id)
                ops = await self.workspace.aupsert(
                    chat_id, user_id, [restored], force_component_id=component_id)
                await self.send_ui_upsert(websocket, chat_id, user_id, ops)
                try:
                    await self.workspace.asnapshot(chat_id, user_id, cause="component_restore")
                except Exception:
                    logger.debug("workspace snapshot failed (component_restore)", exc_info=True)
                try:
                    from audit.hooks import record_workspace_event
                    await record_workspace_event(
                        user_id=user_id, action="component_restored",
                        chat_id=chat_id, component_id=component_id,
                        detail={"restored_version": int(version["version_no"]),
                                "archived_version": archived_no},
                    )
                except Exception:
                    logger.debug("workspace audit failed (component_restored)", exc_info=True)
        except Exception as e:
            logger.error(f"component_restore failed: {e}", exc_info=True)
            await self.send_ui_render(websocket, [
                Alert(message=f"The restore failed: {e}", variant="error").to_dict()
            ], target="chat")
        finally:
            await self._safe_send(websocket, json.dumps({
                "type": "chat_status", "status": "done", "message": ""
            }))

    async def _refine_component_llm(self, websocket, component: Dict[str, Any],
                                    instruction: str) -> Optional[Dict[str, Any]]:
        """Bounded single-purpose LLM edit constrained to the SAME component
        type (research D10): one structured-output call under the user's
        provider config, validated against the renderer registry. Returns
        the refined dict with identity + source metadata carried over and
        provenance re-stamped 'estimated', or ``None`` when the model
        refused / emitted an unusable or type-changing result (the caller
        leaves the component untouched)."""
        from webrender import allowed_primitive_types
        valid_types = set(allowed_primitive_types()) | {"chart"}
        orig_type = str(component.get("type") or "").strip().lower()
        if orig_type not in valid_types:
            return None
        view = {k: v for k, v in component.items()
                if not str(k).startswith("_") and k not in ("component_id", "provenance")}
        comp_json = json.dumps(view)
        if len(comp_json) > 30000:
            logger.info("component_refine: component too large to refine (%d chars)",
                        len(comp_json))
            return None
        source_line = ""
        if component.get("_source_tool"):
            source_line = (
                f"\nIts data came from tool '{component.get('_source_tool')}' of agent "
                f"'{component.get('_source_agent')}' — do not fabricate data that "
                "tool did not provide.")
        prompt = (
            f"Here is a UI component of type '{orig_type}' as JSON:{source_line}\n\n"
            f"{comp_json}\n\n"
            f"Apply this change requested by the user: {instruction}\n\n"
            f'Respond with ONLY the complete edited component as one JSON object. '
            f'It MUST keep "type": "{orig_type}" and remain a valid component of '
            "that type; preserve all data the instruction does not ask you to change."
        )
        result = await self._call_llm_json(
            websocket,
            [
                {"role": "system", "content": (
                    "You are a precise UI component editor. Output ONLY one valid "
                    "JSON object — no explanations, no markdown fences.")},
                {"role": "user", "content": prompt},
            ],
            feature="component_refine", temperature=0.1,
        )
        if not isinstance(result, dict):
            return None
        if isinstance(result.get("component"), dict) and "type" not in result:
            result = result["component"]  # tolerate a {"component": {...}} wrapper
        if str(result.get("type") or "").strip().lower() != orig_type:
            logger.info("component_refine: model changed the component type "
                        "(%r -> %r) — rejected", orig_type, result.get("type"))
            return None
        # The model can neither stamp trust nor mint identities/attribution:
        # strip anything it invented, then carry the original's over.
        refined = {k: v for k, v in result.items() if not str(k).startswith("_")}
        for key in ("provenance", "component_id", "id"):
            refined.pop(key, None)
        self._validate_component_tree(refined, valid_types)
        for key in ("_source_agent", "_source_tool", "_source_params",
                    "_source_correlation_id"):
            if key in component:
                refined[key] = component[key]
        if component.get("id"):
            refined["id"] = component["id"]
        if component.get("component_id"):
            refined["component_id"] = component["component_id"]
        _stamp_provenance(refined, kind="estimated")
        return refined

    async def _reconcile_legacy_replacement(self, websocket, chat_id: str, user_id: str,
                                            *, cause: str):
        """Feature 028 (D18): after a legacy combine/condense replace, stamp
        workspace identities onto the fresh rows, snapshot, and push the full
        live workspace so the mutation is visible (pre-028 the thin client
        silently dropped components_combined/condensed)."""
        if not chat_id:
            return
        try:
            def _stamp_and_snapshot():
                """Stamp identities, snapshot, and materialize off the event loop."""
                now_ms = int(time.time() * 1000)
                for row in self.workspace.live_rows(chat_id, user_id):
                    if row.get("component_id"):
                        continue
                    data = row.get("component_data")
                    if not isinstance(data, dict):
                        continue
                    cid = self.workspace.resolve_identity(data)
                    self.history.db.execute(
                        "UPDATE saved_components SET component_id = ?, component_data = ?, updated_at = ? "
                        "WHERE id = ? AND user_id = ?",
                        (cid, json.dumps(data), now_ms, row["id"], user_id),
                    )
                self.workspace.snapshot(chat_id, user_id, cause=cause)
                return self._canvas_components(chat_id, user_id)

            ws_components = await asyncio.to_thread(_stamp_and_snapshot)
            # FR-040: the replacement is a workspace change — every socket of
            # this user on this chat gets the re-render, not just the
            # originator (REST-initiated calls pass websocket=None).
            targets = [
                ws for ws in self.ui_clients
                if self._get_user_id(ws) == user_id
                and self._ws_active_chat.get(id(ws)) == chat_id
            ]
            if websocket is not None and websocket not in targets:
                targets.append(websocket)
            for ws in targets:
                await self.send_ui_render(ws, ws_components)
        except Exception:
            logger.exception("legacy replacement reconciliation failed (%s)", cause)

    async def _audit_workspace_denial(self, user_id: str, chat_id: str,
                                      component_id: str, reason: str):
        try:
            from audit.hooks import record_workspace_event
            await record_workspace_event(
                user_id=user_id, action="action_denied", chat_id=chat_id,
                component_id=component_id, outcome="failure",
                description=f"Component action denied: {reason}",
                detail={"reason": reason},
            )
        except Exception:
            logger.debug("workspace denial audit failed", exc_info=True)

    async def _readapt_targeted(self, websocket, chat_id, old_profile, new_profile,
                                components: List[Dict]) -> bool:
        """On a viewport/orientation change, re-render each canvas component
        under the old and new profile and push a ``ui_upsert`` for ONLY the
        ones whose fragment actually changed — to THIS socket alone (other
        devices on the chat didn't change). Returns True when handled (even if
        nothing changed — that means the canvas looks identical, no work
        needed), False on any error so the caller falls back to a full
        re-render."""
        try:
            from shared.protocol import UIUpsert
            from webrender import render_component_fragment
            from rote.adapter import ComponentAdapter
            from rote.capabilities import DeviceType

            def _render(comp, profile):
                if profile.device_type == DeviceType.BROWSER:
                    adapted = comp
                else:
                    al = ComponentAdapter.adapt([comp], profile)
                    adapted = al[0] if len(al) == 1 else {"type": "container", "content": al}
                    if isinstance(adapted, dict):
                        adapted["component_id"] = comp.get("component_id")
                return adapted, render_component_fragment(
                    adapted if isinstance(adapted, dict) else comp, profile)

            ops = viewport.targeted_ops(
                components, lambda c: _render(c, old_profile),
                lambda c: _render(c, new_profile))
            if ops:
                await self._safe_send(websocket, UIUpsert(chat_id=chat_id, ops=ops).to_json())
            logger.info("C-D7 targeted re-adapt: %d/%d components changed on chat=%s",
                        len(ops), len(components), chat_id)
            return True
        except Exception:
            logger.exception("C-D7 targeted re-adapt failed; falling back to full render")
            return False

    async def send_ui_upsert(self, websocket, chat_id: str, user_id: str, ops: List[Dict]):
        """Fan a ``ui_upsert`` out to every socket of ``user_id`` whose active
        chat is ``chat_id``, adapting each op per receiving device (D16).

        The structured dict AND its web HTML fragment ride together per op
        (026 FR-018 dual shape); the originating socket goes through the same
        path so there is exactly one delivery code path.
        """
        if not ops:
            return
        from rote.adapter import ComponentAdapter
        from rote.capabilities import DeviceType
        from shared.protocol import UIUpsert
        from webrender import render_component_fragment

        from orchestrator.conversation_publication import (
            current_conversation_publication,
        )

        publication = current_conversation_publication()
        if (
            publication is not None
            and publication.publication_role == "assistant_result"
            and publication.matches(self.history, chat_id, user_id)
        ):
            # A concurrent voice result is a private candidate until its
            # terminal rebase commits. The execution adapter discards this
            # transient projection; real clients receive only the subsequent
            # complete committed snapshot.
            targets = [] if websocket is None else [websocket]
        else:
            targets = [
                ws for ws in self.ui_clients
                if self._get_user_id(ws) == user_id
                and self._ws_active_chat.get(id(ws)) == chat_id
            ]
            if websocket is not None and websocket not in targets:
                targets.append(websocket)

        for ws in targets:
            profile = self.rote.get_profile(ws)
            wire_ops = []
            for op in ops:
                if op.get("op") == "remove":
                    wire_ops.append({"op": "remove", "component_id": op.get("component_id")})
                    continue
                comp = op.get("component")
                cid = op.get("component_id")
                if profile.device_type == DeviceType.BROWSER:
                    adapted = comp
                else:
                    adapted_list = ComponentAdapter.adapt([comp], profile)
                    if len(adapted_list) == 1:
                        adapted = adapted_list[0]
                    else:
                        adapted = {"type": "container", "content": adapted_list}
                    if isinstance(adapted, dict):
                        adapted["component_id"] = cid
                html = None
                try:
                    html = render_component_fragment(
                        adapted if isinstance(adapted, dict) else comp, profile)
                except Exception:
                    logger.exception("webrender: ui_upsert fragment render failed")
                wire_ops.append({"op": "upsert", "component_id": cid,
                                 "component": adapted, "html": html})
            # Feature 051: watch sockets hear the upserted content too. One
            # utterance per delivery, built from the adapted components; the
            # viewport re-adapt path (_readapt_targeted) deliberately does NOT
            # attach speech — re-adapting old content must never re-speak it.
            speech = None
            try:
                from orchestrator.watch_speech import speech_for_profile
                speech = speech_for_profile(profile, [
                    o.get("component") for o in wire_ops
                    if o.get("op") == "upsert" and isinstance(o.get("component"), dict)
                ])
            except Exception:
                logger.debug("watch_speech unavailable for ui_upsert", exc_info=True)
            await self._safe_send(ws, UIUpsert(chat_id=chat_id, ops=wire_ops, speech=speech).to_json())

    async def _handle_tool_progress(self, msg) -> None:
        """Route a long-running job's ToolProgress to the job's CHAT.

        Live updates fan out to every socket the user currently has open on that
        chat (so progress survives a refresh or a move to another device), plus
        the legacy originating socket. On a terminal update the result is
        PERSISTED into the chat workspace so a client returning later
        re-hydrates the completed UI (014/015 + 028). The concurrency-cap slot is
        released on terminal regardless of who is connected.
        """
        md = msg.metadata or {}
        cap_job_id = md.get("cap_job_id", "")
        req_id = md.get("request_id", "")
        phase = md.get("phase", "")
        terminal = bool(md.get("terminal")) or phase in ("completed", "failed", "status_unknown")
        ctx = self._job_context.get(cap_job_id) if cap_job_id else None

        payload: Dict[str, Any] = {
            "type": "tool_progress",
            "tool_name": msg.tool_name,
            "agent_id": msg.agent_id,
            "message": msg.message,
            "percentage": msg.percentage,
        }
        if phase:
            payload["phase"] = phase
        if terminal:
            payload["terminal"] = True
        if md.get("result") is not None:
            payload["result"] = md["result"]

        # Resolve the job-user's CURRENT sockets before terminal finalization.
        # Terminal delivery itself happens only after the atomic conversation
        # update so a following client read can never race the old revision.
        targets: List[Any] = []
        if ctx:
            targets = self._sockets_on_chat(ctx.get("user_id"), ctx.get("chat_id"))
        legacy = self.pending_ui_sockets.get(req_id)
        if legacy is not None and legacy not in targets:
            targets.append(legacy)
        # Terminal: publish result component + narration as one conversation
        # revision before advertising terminal progress. Nonterminal progress
        # remains a disposable status overlay and never touches durable state.
        if terminal and ctx:
            try:
                await self._finalize_long_running_job(ctx, msg)
            except Exception:
                logger.exception("finalizing long-running job failed (cap=%s)", cap_job_id)
                payload["publication_retryable"] = True
            else:
                self._job_context.pop(cap_job_id, None)
        for ws in targets:
            try:
                await self._safe_send(ws, json.dumps(payload))
            except Exception:
                logger.debug("tool_progress forward failed", exc_info=True)
        if cap_job_id and phase in ("completed", "failed", "status_unknown"):
            entry = self._pending_cap_entries.pop(cap_job_id, None)
            if entry:
                u_id, a_id = entry
                try:
                    await self.concurrency_cap.release(u_id, a_id, cap_job_id)
                except Exception:
                    logger.debug("cap release failed", exc_info=True)
            await self._release_hop_cap_slot(cap_job_id)

    def _sockets_on_chat(self, user_id: str, chat_id: str) -> List[Any]:
        """Every socket ``user_id`` currently has open on ``chat_id`` (for
        fanning a job update out to a refreshed / multi-device session)."""
        return [
            ws for ws in self.ui_clients
            if self._get_user_id(ws) == user_id and self._ws_active_chat.get(id(ws)) == chat_id
        ]

    async def _narrate_job_result(self, ctx: Dict[str, Any], result: Dict[str, Any]):
        """Ask the model to narrate a completed job's results (a concise,
        plain-language comparison naming the best performer). Server-initiated
        (operator-default LLM, websocket=None). Returns chat-rail components, or
        None when no LLM is available / the call fails — callers fall back to a
        deterministic note."""
        try:
            tool = ctx.get("tool_name", "the job")
            messages = [
                {"role": "system", "content": (
                    "You summarize a COMPLETED machine-learning training job for the "
                    "user in the chat. Write a concise, plain-language summary (2-4 "
                    "sentences) comparing the models/metrics and naming the best "
                    "performer, using the actual numbers from the results. No preamble, "
                    "no markdown headings, no code fences."
                )},
                {"role": "user", "content": (
                    f"Tool: {tool}\nResults JSON:\n{json.dumps(result, default=str)[:4000]}"
                )},
            ]
            message, _usage = await self._call_llm(
                None, messages, feature="job_summary", temperature=0.3
            )
            text = getattr(message, "content", None) if message is not None else None
            if text and text.strip():
                return self._chat_narrative(text.strip())
        except Exception:
            logger.debug("job result narration failed", exc_info=True)
        return None

    async def _finalize_long_running_job(self, ctx: Dict[str, Any], msg) -> None:
        """Atomically publish a detached job result and its narration.

        The component, assistant turn, complete canvas, revision increment,
        and commit timestamp share one ``conversation_commit`` transaction.
        Current clients receive a commit-ready prelude plus one complete
        snapshot; compatibility overlay frames are emitted only afterward.
        """
        uid = ctx.get("user_id")
        cid = ctx.get("chat_id")
        if not uid or not cid:
            return
        md = msg.metadata or {}
        phase = md.get("phase", "")
        component = self._build_job_result_component(ctx, msg)
        chat_core = None
        if phase == "completed" and isinstance(md.get("result"), dict):
            chat_core = await self._narrate_job_result(ctx, md["result"])
        if not chat_core:
            title = component.get("title") or "Job complete"
            note = title if phase == "completed" else (msg.message or "Job ended.")
            chat_core = [Text(content=f"✓ {note}").to_dict()]

        request_generation = ctx.get("publication_request_generation")
        try:
            parsed_request_generation = _uuid.UUID(str(request_generation))
            if (
                parsed_request_generation.version != 4
                or parsed_request_generation.variant != _uuid.RFC_4122
            ):
                raise ValueError("publication request generation must be UUID4")
            request_generation = str(parsed_request_generation)
        except (TypeError, ValueError, AttributeError):
            request_generation = str(_uuid.uuid4())
            ctx["publication_request_generation"] = request_generation

        locks = getattr(self, "_workspace_locks", None)
        if locks is None:
            locks = {}
            self._workspace_locks = locks
        lock = locks.setdefault(cid, asyncio.Lock())
        ops: List[Dict[str, Any]] = []
        stage = None
        token = None
        async with lock:
            try:
                stage, token = await self._begin_detached_conversation_publication(
                    chat_id=cid,
                    user_id=uid,
                    request_generation=request_generation,
                )
                ops = await self.workspace.aupsert(cid, uid, [component])
                await self._append_conversation_message(
                    stage,
                    chat_id=cid,
                    user_id=uid,
                    role="assistant",
                    content=chat_core,
                )
                await self._publish_conversation_snapshot(
                    None,
                    stage=stage,
                    request_generation=request_generation,
                    server_initiated=True,
                )
            finally:
                if stage is not None and not stage.sealed:
                    try:
                        await asyncio.to_thread(
                            self.conversation_commits.abort_commit,
                            commit_id=stage.commit_id,
                            owner_user_id=stage.user_id,
                        )
                        stage.seal(committed=False)
                    except Exception:
                        logger.warning(
                            "detached conversation stage abort failed",
                            exc_info=True,
                        )
                if token is not None:
                    from orchestrator.conversation_publication import (
                        reset_conversation_publication,
                    )

                    reset_conversation_publication(token)

        component_id = component.get("component_id")
        # Bounded compatibility overlays remain non-authoritative for 060
        # reducers; legacy clients still receive the familiar live update.
        await self.send_ui_upsert(None, cid, uid, ops)
        for ws in self._sockets_on_chat(uid, cid):
            try:
                await self.send_ui_render(ws, chat_core, target="chat")
            except Exception:
                logger.debug("job narration live delivery failed", exc_info=True)

        try:
            from audit.hooks import record_workspace_event
            await record_workspace_event(
                user_id=uid, action="component_added", chat_id=cid,
                component_id=component_id, outcome="success",
                description=f"Long-running job result delivered: {ctx.get('tool_name')}",
            )
        except Exception:
            logger.debug("job finalize audit failed", exc_info=True)

    def _build_job_result_component(self, ctx: Dict[str, Any], msg) -> Dict[str, Any]:
        """Build a deterministic result component from a terminal ToolProgress.

        A completed job renders its metrics as a Table; a failed / unknown
        outcome renders a status Alert. The component is source-tagged so the
        workspace assigns it a stable identity (028) and it survives reload."""
        md = msg.metadata or {}
        phase = md.get("phase", "")
        tool = ctx.get("tool_name", "job")
        source = {
            "_source_agent": ctx.get("agent_id"),
            "_source_tool": ctx.get("tool_name"),
            "_source_params": {"_job_result": ctx.get("chat_id", "")},
        }
        if phase == "completed":
            rows: List[List[str]] = []

            def _flatten(d: Dict[str, Any], prefix: str = "") -> None:
                for k, v in d.items():
                    key = f"{prefix}{k}"
                    if isinstance(v, (str, int, float, bool)) or v is None:
                        rows.append([key, "" if v is None else str(v)])
                    elif isinstance(v, dict) and len(rows) < 40:
                        _flatten(v, f"{key}.")

            result = md.get("result")
            if isinstance(result, dict):
                _flatten(result)
            comp: Dict[str, Any] = {
                "type": "table",
                "title": f"Training complete — {tool}",
                "headers": ["Metric", "Value"],
                "rows": rows[:40] or [["status", "complete"]],
            }
        else:
            note = msg.message or ("Job failed." if phase == "failed" else "Job status unknown.")
            comp = {
                "type": "alert",
                "message": f"{tool}: {note}",
                "variant": "error" if phase == "failed" else "warning",
            }
        comp.update(source)
        return comp

    async def _retire_welcome_canvas(self, websocket):
        """First chat_message from a welcome-showing socket: pop the flag.

        055 FF_FIRST_TURN_CONTRACT (on): clients purge the wel_-identified
        welcome components locally at turn start, so no frame is sent — the
        legacy blanking ``ui_render []`` reached the client one RTT after
        send and destroyed its optimistic loading skeleton (US1 root cause).
        Flag off restores the legacy blanking frame byte-for-byte. The
        ``_ws_welcome`` bookkeeping pops either way (the
        enable_recommended_agents welcome refresh keys on it).
        """
        if self._ws_welcome.pop(id(websocket), None):
            if not flags.is_enabled("first_turn_contract"):
                try:
                    await self.send_ui_render(websocket, [])
                except Exception:
                    pass

    async def send_ui_render(self, websocket, components: List, target: str = "canvas",
                             speak: bool = True):
        """Send a UIRender message to a UI client, adapted via ROTE.

        ``speak=False`` suppresses the watch spoken rendition for renders that
        re-present EXISTING content (chat re-hydration, viewport re-adaptation,
        the welcome canvas) — a turn is never auto-spoken twice (FR-030)."""
        # Auto-route alert-only messages (any variant) to the chat panel
        # instead of the canvas — a frame that is nothing but alerts would
        # otherwise clobber the workspace with a partial single-alert render.
        if target == "canvas" and components and all(
            isinstance(c, dict) and c.get("type") == "alert"
            for c in components
        ):
            target = "chat"
        adapted = self.rote.adapt(websocket, components)
        html = None
        try:
            profile = self.rote.get_profile(websocket)
            if target == "canvas":
                # Feature 028: canvas renders carry per-component identity
                # wrappers so every top-level component is a ui_upsert morph
                # target (contracts/ws-workspace-protocol.md).
                from webrender import render_workspace
                html = render_workspace(adapted, profile)
            else:
                from webrender import render_for_target, target_for_profile
                # Per-device renderer target — voice/aom native targets when
                # FF_NATIVE_TARGETS is on, web otherwise (the registry seam).
                html = render_for_target(target_for_profile(profile), adapted, profile)
        except Exception:
            logger.exception("webrender: failed to render UI (sending structured components only)")
        # Feature 051: watch-profile sockets additionally hear the delivery —
        # spoken rendition of the SAME adapted components (fail-open, absent
        # for every other profile; contracts/spoken-rendition.md).
        speech = None
        if speak:
            try:
                from orchestrator.watch_speech import speech_for_profile
                speech = speech_for_profile(self.rote.get_profile(websocket), adapted)
            except Exception:
                logger.debug("watch_speech unavailable for ui_render", exc_info=True)
        msg = UIRender(components=adapted, target=target, html=html, speech=speech)
        await self._safe_send(websocket, msg.to_json())
        # 055 bg-continuity: a background (VirtualWebSocket) turn's chat-rail
        # narrative also reaches the user's real sockets on that chat, each
        # re-adapted per device (rich canvas components already fan through
        # the workspace upsert path). Real-socket sends fan nowhere — checked
        # inline so the method stays self-contained for them (DB-free tests
        # bind it standalone).
        if target == "chat":
            from orchestrator.async_tasks import VirtualWebSocket
            if isinstance(websocket, VirtualWebSocket):
                for ws in self._vws_fan_targets(websocket):
                    try:
                        await self.send_ui_render(ws, components, target="chat", speak=speak)
                    except Exception:  # pragma: no cover - per-socket best-effort
                        logger.debug("vws chat-rail fan failed", exc_info=True)

    async def _shell_token_for_request(self, request):
        """Feature 026: access token for the web shell's WS register_ui handshake.
        Server-side OIDC session (or 'dev-token' under mock auth)."""
        try:
            from orchestrator.web_auth import session_token
            return await asyncio.to_thread(session_token, request)
        except Exception:
            logger.debug("web_auth: session_token unavailable", exc_info=True)
            return ""

    @staticmethod
    def _adapt_tool_schema_for_model(schema: dict) -> dict:
        """Adapt an already-validated schema for the model function grammar.

        Registration is the validation boundary. This downstream projection
        does not repair invalid schemas; it only removes the dialect annotation
        that some model providers reject.
        """
        if not isinstance(schema, dict):
            raise ProtocolValidationError("validated tool schema must be an object")
        adapted = dict(schema)
        adapted.pop("$schema", None)
        return adapted

    def _is_draft_agent(self, agent_id: str) -> bool:
        """Hide any agent whose agent_id maps to a non-live draft record.

        Feature 030: an explicitly PUBLIC agent is never treated as a draft.
        Lifecycle drafts are always private, so a public ownership row means
        the slug-reverse draft match was a stale-row false positive — e.g.
        a live bundled agent (such as ``weather-1``) whose directory name
        collides with an old draft slug and was silently hidden from the
        agent list and Public tab (verified walkthrough finding).
        """
        if hasattr(self, 'lifecycle_manager'):
            draft = self.lifecycle_manager._find_draft_by_agent_id(agent_id)
            if draft and draft["status"] != "live":
                try:
                    ownership = self.history.db.get_agent_ownership(agent_id) or {}
                except Exception:
                    ownership = {}
                if bool(ownership.get("is_public", False)):
                    return False
                return True
        return False

    def _build_dashboard_agent_list(self, user_id: str) -> List[Dict[str, Any]]:
        """Assemble the per-user dashboard agent entries (sync DB reads —
        callers on the event loop run this via ``asyncio.to_thread``)."""
        ownership_map = {o["agent_id"]: o for o in self.history.db.get_all_agent_ownership()}
        agent_list = []
        for agent_id, card in self.agent_cards.items():
            # Hide draft agents that aren't live yet — they only appear in the Drafts tab
            if self._is_draft_agent(agent_id):
                continue
            available_tools = [s.id for s in card.skills]
            scopes = self.tool_permissions.get_agent_scopes(user_id, agent_id)
            tool_scope_map = self.tool_permissions.get_tool_scope_map(agent_id)
            permissions = self.tool_permissions.get_effective_permissions(
                user_id, agent_id, available_tools
            )
            ownership = ownership_map.get(agent_id, {})
            entry = {
                "id": card.agent_id,
                "name": card.name,
                "description": card.description,
                "tools": available_tools,
                "tool_descriptions": {s.id: s.description for s in card.skills},
                "scopes": scopes,
                "tool_scope_map": tool_scope_map,
                "permissions": permissions,
                "security_flags": self.security_flags.get(agent_id, {}),
                "status": "connected",
                "owner_email": ownership.get("owner_email"),
                "is_public": bool(ownership.get("is_public", False)),
            }
            if getattr(card, 'metadata', None):
                entry["metadata"] = card.metadata
            agent_list.append(entry)
        return agent_list

    def get_personal_agent_capabilities(self) -> Dict[str, Any]:
        """Return one detached candidate-owned capability payload.

        REST and WebSocket configuration both call this getter so candidate
        applicability cannot drift between surfaces or be mutated by a caller.
        """

        return self.personal_agent_capabilities.to_dict()["capabilities"]

    async def send_dashboard(self, websocket):
        """Send the initial dashboard view."""
        user_id = self._get_user_id(websocket)
        agent_list = await asyncio.to_thread(self._build_dashboard_agent_list, user_id)

        # Calculate total available tools for this user based on permissions
        total_tools = 0
        for agent in agent_list:
            if "permissions" in agent:
                total_tools += sum(1 for v in agent["permissions"].values() if v)
            else:
                total_tools += len(agent["tools"])

        # Build streamable tools list for live streaming. Includes BOTH the
        # legacy poll path (gated by FF_LIVE_STREAMING) AND the new push path
        # (001-tool-stream-ui, gated by FF_TOOL_STREAMING). The frontend
        # uses the `kind` field to decide which subscribe payload to send.
        streamable_list: Dict[str, Dict[str, Any]] = {}
        live_enabled = flags.is_enabled("live_streaming")
        push_enabled = flags.is_enabled("tool_streaming")
        for tool_name, cfg in self._streamable_tools.items():
            kind = cfg.get("kind", "poll")
            if kind == "push" and not push_enabled:
                continue
            if kind == "poll" and not live_enabled:
                continue
            streamable_list[tool_name] = {
                "agent_id": cfg["agent_id"],
                "default_interval": cfg.get("default_interval", 5),
                "kind": kind,
            }

        await self._safe_send(websocket, json.dumps({
            "type": "system_config",
            "config": {
                "agents": agent_list,
                "total_tools": total_tools,
                "streamable_tools": streamable_list,
                "capabilities": self.get_personal_agent_capabilities(),
            }
        }))

    def compute_tools_available_for_user(
        self, user_id: str, draft_agent_id: Optional[str] = None
    ) -> bool:
        """Return ``True`` iff at least one tool is currently dispatchable for ``user_id``.

        Mirrors the per-turn filter loop in :meth:`handle_chat_message`
        (registered agent + system security_flags + per-user
        :meth:`tool_permissions.is_tool_allowed`). Used by feature 008
        for two purposes:

        1. The orchestrator decides whether the next chat turn enters
           the text-only branch (caller passes ``draft_agent_id`` so a
           draft test chat is scoped correctly per FR-010).
        2. :meth:`send_agent_list` broadcasts the result as
           ``tools_available_for_user`` so the frontend can mount the
           persistent text-only banner (FR-007a).

        Args:
            user_id: The user whose permissions gate tool availability.
            draft_agent_id: When set, only that agent's tools are
                considered (matches the dispatch-time draft scoping).
                When ``None``, every connected non-draft agent is
                considered.

        Returns:
            ``True`` if at least one tool would survive the full filter
            stack for ``user_id``. ``False`` otherwise — this is the
            signal that the chat turn would dispatch in text-only mode.
        """
        for agent_id, card in self.agent_cards.items():
            if agent_id not in self.agents and agent_id not in self.local_agents:
                continue
            if draft_agent_id and agent_id != draft_agent_id:
                continue
            agent_flags = self.security_flags.get(agent_id, {})
            for skill in card.skills:
                if skill.id in agent_flags and agent_flags[skill.id].get("blocked"):
                    continue
                if not self.tool_permissions.is_tool_allowed(user_id, agent_id, skill.id):
                    continue
                return True
        return False

    def _enable_recommended_agent_scopes(self, user_id: str,
                                         requested_agent_ids=None) -> List[str]:
        """Consent-based bulk enable for the public catalog (feature 030).

        For every connected, non-draft, PUBLIC agent (optionally narrowed to
        ``requested_agent_ids``), grants the scopes its registered tools
        actually use — minus ``tools:write``, which is never granted here
        (Constitution VII: attenuated, system-computed scopes; explicit user
        click as the grant). Unknown, private, or draft ids are silently
        ignored. Returns the agent ids that were enabled.
        """
        ownership_map = {o["agent_id"]: o for o in self.history.db.get_all_agent_ownership()}
        enabled: List[str] = []
        for agent_id in list(self.agent_cards.keys()):
            if requested_agent_ids is not None and agent_id not in requested_agent_ids:
                continue
            if self._is_draft_agent(agent_id):
                continue
            if not bool(ownership_map.get(agent_id, {}).get("is_public", False)):
                continue
            scopes = self.tool_permissions.scopes_required_by_tools(agent_id)
            if not scopes:
                continue
            self.tool_permissions.set_agent_scopes(
                user_id, agent_id, {s: True for s in scopes})
            enabled.append(agent_id)
        if enabled:
            logger.info(
                f"Consent enable (030): user={user_id} agents={enabled} (write excluded)")
        return enabled

    # 030: chat rail vs canvas split — the chat bubble stays concise words;
    # long/structured narrative content is promoted to a durable canvas card.
    _NARRATIVE_PROMOTE_CHARS = 700

    @classmethod
    def _narrative_is_long(cls, content: str) -> bool:
        """True when a final narrative is too long/structured for the chat rail."""
        c = content or ""
        return (len(c) > cls._NARRATIVE_PROMOTE_CHARS
                or bool(re.search(r"(?m)^#{1,6}\s", c))
                or bool(re.search(r"(?m)^\|.+\|\s*$", c)))

    @staticmethod
    def _concise_lead(content: str, limit: int = 320) -> str:
        """First plain sentences of a narrative — headings/tables stripped."""
        lines = [ln for ln in (content or "").splitlines()
                 if ln.strip() and not ln.lstrip().startswith(("#", "|", ">"))]
        text = " ".join(" ".join(lines).split())
        if not text:
            text = " ".join((content or "").split())
        if len(text) <= limit:
            return text
        cut = text[:limit]
        for sep in (". ", "! ", "? "):
            idx = cut.rfind(sep)
            if idx > 80:
                return cut[:idx + 1]
        return cut.rsplit(" ", 1)[0] + "…"

    @staticmethod
    def _narrative_doc_card(chat_id: str, content: str) -> Dict[str, Any]:
        """Durable canvas card for a long-form narrative (drafts, documents).

        Identity is derived from the chat and the document's own first
        heading, so iterating on the same document ("revise the aims")
        SUPERSEDES it in place while a different document appends — the
        walkthrough found grant deliverables vanishing into chat scroll with
        no workspace identity at all.
        """
        import hashlib
        text = _strip_toolcall_leakage(content)
        if not text and (content or "").strip():
            _log_stripped_empty("doc_card", chat_id, content)
            text = _LEAK_FALLBACK_TEXT
        m = re.search(r"(?m)^#{1,6}\s+(.+)$", text)
        title = (m.group(1).strip()[:120] if m else "Document")
        digest = hashlib.sha1(f"{chat_id}|{title}".encode("utf-8")).hexdigest()[:12]
        return Card(id=f"doc_{digest}", title=title, content=[
            Text(content=text, variant="markdown"),
        ]).to_dict()

    @staticmethod
    def _provenance_caption(tools_ran: bool) -> Dict[str, Any]:
        """Deterministic provenance chip for chat replies (feature 030).

        The walkthrough found clinical/grant prose presented authoritatively
        with no provenance at all. This caption is server-composed (never
        left to the model) and distinguishes model-memory answers from
        tool-grounded ones. Renderer-level honesty, model-independent.
        """
        if tools_ran:
            text = "Based on this turn's tool results — sources and steps are shown above."
        else:
            text = ("Model knowledge only — no live tools or sources were used in this "
                    "reply. Verify independently before relying on it.")
        return Text(content=text, variant="caption").to_dict()

    async def _notify_phi_if_detected(self, websocket, chat_id: str,
                                      user_id: str, message: str) -> None:
        """Notify-only PHI awareness for chat input (feature 030).

        Fires a transient chat Alert when a user message LOOKS like it
        contains PHI. Detection is fail-open (a missing/erroring analyzer
        never fires the notice) and the persistence posture is unchanged:
        the message stays in the transcript, cross-session memory keeps its
        fail-closed Presidio gate, and audit stays content-free. Shown at
        most once per chat per socket. Never raises into the chat turn.
        """
        try:
            if not message or not chat_id:
                return
            if not hasattr(self, "_phi_notified"):
                self._phi_notified = set()
            key = (id(websocket), chat_id)
            if key in self._phi_notified:
                return
            from personalization.phi_gate import get_phi_gate
            gate = get_phi_gate()
            hit = await asyncio.to_thread(gate.detect_for_notice, message)
            if not hit:
                return
            self._phi_notified.add(key)
            logger.info(f"phi_notice.shown chat_id={chat_id}")  # content-free
            await self.send_ui_render(websocket, [Alert(
                title="Possible PHI in your message",
                message=("This looks like it may contain protected health information. "
                         "It stays in this chat's transcript only — it is never added to "
                         "cross-session memory, and audit logs record message lengths, "
                         "not content. Prefer synthetic or de-identified data where "
                         "possible."),
                variant="warning",
            ).to_dict()], target="chat")
        except Exception:
            logger.debug("phi notice failed (non-fatal)", exc_info=True)

    def _text_only_cta_components(self, user_id: str) -> List[Dict[str, Any]]:
        """Deterministic enable affordance for text-only replies (feature 030).

        Appended server-side to the chat reply when a turn dispatched with
        zero tools AND the user has never enabled any agent scope. Users who
        deliberately disabled their agents (rows exist, some enabled
        elsewhere) are not nagged. Composed of astralprims primitives per
        Constitution II/VIII; the buttons route through the audited
        ``enable_recommended_agents`` / ``chrome_open`` actions.

        Since feature 040 this no longer fires for a fresh account — the safe
        baseline makes the built-ins dispatchable, so such a turn is not
        text-only in the first place and never reaches here. That is correct,
        not a regression: see :func:`orchestrator.welcome.enable_agents_card`
        for the populations still reachable (explicit opt-out,
        ``FF_SAFE_AGENTS`` off, safe-but-private catalog).
        """
        try:
            if self.tool_permissions.has_any_enabled_scope(user_id):
                return []
        except Exception:
            return []
        return [
            Alert(
                message=("Answered without agents — live search, data and "
                         "interactive components are currently off for this "
                         "account."),
                variant="info",
            ).to_dict(),
            Button(label="Enable recommended agents",
                   action="enable_recommended_agents",
                   payload={"source": "text_only"}).to_dict(),
            Button(label="Choose agents individually", action="chrome_open",
                   payload={"surface": "agents"}, variant="secondary").to_dict(),
        ]

    async def send_agent_list(self, websocket):
        """Send list of connected agents."""
        user_id = self._get_user_id(websocket)

        def _build_agent_list():
            """Assemble the per-user agent entries off the event loop."""
            ownership_map = {o["agent_id"]: o for o in self.history.db.get_all_agent_ownership()}
            latest_runtime_by_agent: dict[str, Any] = {}
            loader = getattr(
                getattr(self, "personal_agent_runtime", None),
                "list_latest_runtime_instances",
                None,
            )
            if callable(loader):
                try:
                    latest_runtime_by_agent = {
                        runtime.fence.agent_id: runtime
                        for runtime in loader(owner_user_id=user_id)
                    }
                except Exception:
                    logger.warning(
                        "Personal-agent lifecycle list hydration failed",
                        exc_info=True,
                    )
            agents = []
            for agent_id, card in self.agent_cards.items():
                # Hide draft agents that aren't live yet
                if self._is_draft_agent(agent_id):
                    continue
                available_tools = [s.id for s in card.skills]
                scopes = self.tool_permissions.get_agent_scopes(user_id, agent_id)
                tool_scope_map = self.tool_permissions.get_tool_scope_map(agent_id)
                permissions = self.tool_permissions.get_effective_permissions(
                    user_id, agent_id, available_tools
                )
                ownership = ownership_map.get(agent_id, {})
                runtime = latest_runtime_by_agent.get(agent_id)
                lifecycle_state = (
                    self._personal_agent_lifecycle_from_runtime(runtime)[0]
                    if runtime is not None
                    else "connected"
                )
                entry = {
                    "id": card.agent_id,
                    "name": card.name,
                    "description": card.description,
                    "tools": [{"name": s.id, "description": s.description} for s in card.skills],
                    "scopes": scopes,
                    "tool_scope_map": tool_scope_map,
                    "permissions": permissions,
                    "security_flags": self.security_flags.get(agent_id, {}),
                    "status": lifecycle_state,
                    "owner_email": ownership.get("owner_email"),
                    "is_public": bool(ownership.get("is_public", False)),
                }
                if getattr(card, 'metadata', None):
                    entry["metadata"] = card.metadata
                agents.append(entry)
            return agents

        agents = await asyncio.to_thread(_build_agent_list)

        # Feature 008-llm-text-only-chat (FR-007a, contracts/ws-agent-list.md).
        # Broadcast a single boolean for this user that collapses the
        # three reasons a chat would dispatch in text-only mode (no
        # agents connected, all tools blocked by user permissions, all
        # blocked by security flags). The frontend uses this to toggle
        # the persistent text-only banner.
        tools_available_for_user = await asyncio.to_thread(
            self.compute_tools_available_for_user, user_id)

        sent = await self._safe_send(websocket, json.dumps({
            "type": "agent_list",
            "tools_available_for_user": tools_available_for_user,
            "agents": agents,
        }))
        if sent and isinstance(user_id, str) and user_id:
            try:
                await self._replay_personal_agent_lifecycles(websocket, user_id)
            except Exception:
                logger.warning(
                    "Personal-agent lifecycle replay failed closed",
                    exc_info=True,
                )

    # =========================================================================
    # SERVER
    # =========================================================================

    async def handle_ui_connection_fastapi(self, websocket: WebSocket):
        """Handle a UI client WebSocket connection using FastAPI."""
        await websocket.accept()
        self.ui_clients.append(websocket)
        self._registered_events[id(websocket)] = asyncio.Event()
        logger.info(f"UI client connected (total: {len(self.ui_clients)})")

        # Hook: SESSION_START
        if flags.is_enabled("hook_system"):
            await self.hooks.emit(HookContext(
                event=HookEvent.SESSION_START,
                metadata={"websocket_id": id(websocket)},
            ))

        try:
            await self._serve_ui_frames(websocket, websocket.receive_text)
        except WebSocketDisconnect:
            logger.info("UI client disconnected")
        except Exception as e:
            # Only log interesting errors
            if "ConnectionClosed" not in str(e):
                logger.error(f"WebSocket error: {e}")
        finally:
            # Hook: SESSION_END
            if flags.is_enabled("hook_system"):
                user_data = self.ui_sessions.get(websocket, {})
                await self.hooks.emit(HookContext(
                    event=HookEvent.SESSION_END,
                    user_id=user_data.get("user_id", ""),
                    metadata={"websocket_id": id(websocket)},
                ))

            # Audit: WebSocket logout / disconnect
            try:
                _claims = self.ui_sessions.get(websocket)
                if _claims:
                    from audit.hooks import record_auth_event
                    await record_auth_event(
                        claims=_claims,
                        action="ws_disconnect",
                        description="WebSocket session ended",
                    )
            except Exception as _e:
                logger.debug(f"WS disconnect audit record failed: {_e}")

            self._cleanup_streams(websocket)
            # 001-tool-stream-ui (US2 T043): pause any push streams owned by
            # this websocket. They transition to DORMANT and become eligible
            # for resume on the user's return (US3).
            if self.stream_manager is not None:
                try:
                    await self.stream_manager.detach(websocket)
                except Exception as e:
                    logger.warning(f"stream_manager.detach failed: {e}")
            # Persist host loss and settle exact fenced calls before removing
            # authentication/socket projections. Legacy tunnel teardown below
            # remains a separate compatibility path.
            try:
                await self._disconnect_personal_agent_host(websocket)
            except Exception:
                logger.debug("personal-agent host teardown failed", exc_info=True)
            # 058 (honest-offline): take this socket's tunneled user agents
            # offline BEFORE dropping its session (teardown reads the owner sub
            # from ui_sessions). No-op when no user agent is tunneled here.
            try:
                await self._teardown_owner_tunnels(websocket)
            except Exception:
                logger.debug("user-agent tunnel teardown failed", exc_info=True)
            self._ws_active_chat.pop(id(websocket), None)
            getattr(self, "_conversation_scopes", {}).pop(id(websocket), None)
            self._ws_timeline_mode.pop(id(websocket), None)
            self._ws_welcome.pop(id(websocket), None)
            if websocket in self.ui_clients:
                self.ui_clients.remove(websocket)
            self._clear_voice_control_binding(websocket)
            if websocket in self.ui_sessions:
                del self.ui_sessions[websocket]
            self._chat_locks.pop(id(websocket), None)
            self._registered_events.pop(id(websocket), None)
            (getattr(self, "_agent_host_sockets", None) or {}).pop(id(websocket), None)
            # Feature 054: persisted LLM config SURVIVES disconnect by design;
            # only the per-socket gate marker is dropped.
            from orchestrator import llm_gate as _llm_gate
            _llm_gate.clear_socket(self, websocket)
            self.rote.cleanup(websocket)
            logger.info(f"UI client session cleaned up (total: {len(self.ui_clients)})")

    async def handle_ui_connection(self, websocket, path=None):
        """Handle a UI client WebSocket connection (legacy websockets lib)."""
        self.ui_clients.append(websocket)
        self._registered_events[id(websocket)] = asyncio.Event()
        logger.info(f"UI client connected (total: {len(self.ui_clients)})")
        try:
            if hasattr(websocket, "recv"):
                receive = websocket.recv
            else:
                receive = websocket.__aiter__().__anext__
            await self._serve_ui_frames(websocket, receive)
        except (websockets.exceptions.ConnectionClosed, StopAsyncIteration):
            logger.info("UI client disconnected")
        finally:
            self._cleanup_streams(websocket)
            # 001-tool-stream-ui (US2 T043): same detach for the legacy path.
            if self.stream_manager is not None:
                try:
                    await self.stream_manager.detach(websocket)
                except Exception as e:
                    logger.warning(f"stream_manager.detach failed: {e}")
            try:
                await self._disconnect_personal_agent_host(websocket)
            except Exception:
                logger.debug("personal-agent host teardown failed", exc_info=True)
            # 058 (honest-offline): take this socket's tunneled user agents
            # offline BEFORE dropping its session (teardown reads the owner sub
            # from ui_sessions). No-op when no user agent is tunneled here.
            try:
                await self._teardown_owner_tunnels(websocket)
            except Exception:
                logger.debug("user-agent tunnel teardown failed", exc_info=True)
            self._ws_active_chat.pop(id(websocket), None)
            getattr(self, "_conversation_scopes", {}).pop(id(websocket), None)
            self._ws_timeline_mode.pop(id(websocket), None)
            self._ws_welcome.pop(id(websocket), None)
            if websocket in self.ui_clients:
                self.ui_clients.remove(websocket)
            self._clear_voice_control_binding(websocket)
            if websocket in self.ui_sessions:
                del self.ui_sessions[websocket]
            self._chat_locks.pop(id(websocket), None)
            self._registered_events.pop(id(websocket), None)
            (getattr(self, "_agent_host_sockets", None) or {}).pop(id(websocket), None)
            # Feature 054: persisted LLM config SURVIVES disconnect by design;
            # only the per-socket gate marker is dropped.
            from orchestrator import llm_gate as _llm_gate
            _llm_gate.clear_socket(self, websocket)
            self.rote.cleanup(websocket)
            logger.info(f"UI client session cleaned up (total: {len(self.ui_clients)})")

    async def start(self):
        logger.info(f"Orchestrator starting on port {PORT}")

        # Feature 028 (FR-015): production posture is fail-closed. Mock auth
        # outside explicitly declared development mode is a fatal
        # misconfiguration — refuse to serve rather than run open.
        from orchestrator.session_store import assert_production_posture
        assert_production_posture()

        # Feature 040 (US2): mark the bundled first-party fleet owner-safe so
        # their tools are usable out of the box (audited; idempotent — already-
        # safe agents are skipped on re-boot). Gated by FF_SAFE_AGENTS.
        try:
            from shared.feature_flags import flags
            if flags.is_enabled("safe_agents"):
                from orchestrator import agent_trust
                seed_ids = self.history.db._FIRST_PARTY_PUBLIC_AGENT_IDS
                # Feature 063 (FR-002/FR-004/FR-005): the unified remote-compute-1 is
                # safe-seeded ONLY when the remote-compute feature is enabled — so with
                # the flag off the seed set is byte-identical to the pre-063 fleet (no
                # agent_trust row or audit event for it). Safe-seeding flips only the
                # baseline; every DESTRUCTIVE verb is still gated per-verb by the
                # confirmation mechanism (remote_confirmation) no matter the baseline.
                if not flags.is_enabled("remote_compute"):
                    seed_ids = tuple(a for a in seed_ids if a != "remote-compute-1")
                await agent_trust.seed_safe(self.history.db, seed_ids)
        except Exception:
            logger.debug("Feature 040 safe seed failed (non-fatal)", exc_info=True)

        # Feature 040 (US1): register the bundled first-party agents IN-PROCESS
        # (no per-agent uvicorn port). start.py skips spawning them as
        # subprocesses when this flag is on; the networked path remains for any
        # agent not registered locally. Default ON; kill-switch falls back to WS.
        try:
            if flags.is_enabled("inprocess_agents"):
                from orchestrator import local_agents
                await local_agents.register_built_ins(self)
        except Exception:
            logger.exception("Feature 040: in-process agent registration failed")

        # Feature 028 (FR-013): drain queued offline sign-out revocations.
        async def _revocation_queue_loop():
            from orchestrator.web_auth import process_revocation_queue_once
            interval = int(os.getenv("AUTH_REVOCATION_RETRY_SECONDS", "60"))
            while True:
                try:
                    resolved = await process_revocation_queue_once()
                    if resolved:
                        logger.info("auth: resolved %d queued credential revocation(s)", resolved)
                except Exception:
                    logger.debug("auth: revocation queue pass failed", exc_info=True)
                await asyncio.sleep(interval)

        asyncio.create_task(_revocation_queue_loop())

        # Feature 052 (FR-011): warm the IdP signing keys at boot and keep
        # them fresh in the background so interactive token validation never
        # pays a cold JWKS fetch. Never blocks boot or /readyz.
        asyncio.create_task(self._jwks_warm_loop())

        if (
            self._personal_agent_watchdog_task is None
            or self._personal_agent_watchdog_task.done()
        ):
            self._personal_agent_watchdog_task = asyncio.create_task(
                self._personal_agent_watchdog_loop(),
                name="personal-agent-runtime-watchdog",
            )

        # Feature 063 US4: launch the remote-cluster-job poller ONLY when the
        # remote-compute feature is on (fail-closed — with the flag off no task is
        # created and boot is byte-identical to today). Read-only status polling
        # needs only the remote_compute gate, not FF_SCHEDULER_EXECUTION.
        if flags.is_enabled("remote_compute") and (
            self._remote_job_poll_task is None or self._remote_job_poll_task.done()
        ):
            self._remote_job_poll_task = asyncio.create_task(
                self._remote_job_poll_loop(), name="remote-cluster-job-poller")

        # Feature 052 (FR-028): pre-load the PHI analyzer singleton in a
        # daemon thread so the first personalization write doesn't stall on
        # the 2-5 s Presidio+spaCy build. Readiness never waits on it.
        self._start_phi_warm()

        # 030: purge permission rows leaked by drafts discarded before the
        # delete-time purge existed (run in a thread — pure DB/dir checks).
        try:
            purged = await asyncio.to_thread(
                self.lifecycle_manager.reconcile_orphaned_draft_permissions)
            if purged:
                logger.info("Startup sweep purged leaked draft permissions "
                            f"for {purged} agent id(s)")
        except Exception:
            logger.debug("draft permission sweep failed (non-fatal)", exc_info=True)

        # Auto-discover agents (continuous monitor)
        agent_port = int(os.getenv("AGENT_PORT", 8003))
        max_agents = int(os.getenv("MAX_AGENTS", 10))
        asyncio.create_task(self._monitor_agents(agent_port, max_agents))

        # Start knowledge synthesis background loop
        if flags.is_enabled("knowledge_synthesis") and hasattr(self, '_knowledge_synthesizer'):
            asyncio.create_task(self._knowledge_synthesizer.run_loop())

        # Feature 004 — daily quality-signal job + proposal generation
        async def _feedback_quality_loop():
            from feedback.quality import compute_for_window
            from feedback.proposals import generate_for_underperforming
            interval_seconds = int(os.getenv("FEEDBACK_QUALITY_JOB_INTERVAL", str(24 * 3600)))
            # First run after a short warm-up so a freshly-restarted server
            # produces an initial snapshot quickly without colliding with startup.
            await asyncio.sleep(int(os.getenv("FEEDBACK_QUALITY_JOB_WARMUP", "60")))
            while True:
                try:
                    await compute_for_window(self.feedback_repo)
                    refine = None
                    if flags.is_enabled("knowledge_synthesis") and getattr(self, "_knowledge_synthesizer", None):
                        refine = getattr(self._knowledge_synthesizer, "refine_proposal", None)
                    await generate_for_underperforming(self.feedback_repo, refine_with_llm=refine)
                except Exception as exc:
                    logger.warning("feedback quality loop iteration failed: %s", exc)
                await asyncio.sleep(interval_seconds)

        asyncio.create_task(_feedback_quality_loop())

        # Feature 025 wiring (027 click-through finding): the scheduler loop
        # was never instantiated anywhere, so cron jobs and "Run now" silently
        # never dispatched.
        #
        # 030-finish-soul-integration (FR-005, Constitution VII): the EXECUTION
        # loop runs unattended jobs under the offline-grant store, so it is now
        # FAIL-CLOSED — it starts only when FF_SCHEDULER_EXECUTION is enabled,
        # which MUST NOT be turned on until the lead-dev security review of
        # offline_grant.py is recorded (030 FR-004 / 025 T057). When the gate is
        # off, no job-execution code path is reachable; chat-side scheduling
        # (proposals/consent cards) is unaffected and the surface reports
        # unattended execution as unavailable.
        if flags.is_enabled("scheduler_execution"):
            try:
                from orchestrator.offline_grant import OfflineGrantStore
                from scheduler.loop import SchedulerLoop
                from scheduler.runner import JobRunner
                from scheduler.store import ScheduledJobStore
                _job_store = ScheduledJobStore(
                    self.history.db,
                    coordinator=self.work_admission,
                )
                _job_runner = JobRunner(self, _job_store, OfflineGrantStore(self.history.db))
                self._scheduler_loop = SchedulerLoop(
                    _job_store,
                    _job_runner,
                    self.async_task_manager,
                    coordinator=self.work_admission,
                )
                self._scheduler_loop.start()
                logger.info("scheduler.execution_loop_started (FF_SCHEDULER_EXECUTION=on)")
            except Exception:
                logger.exception("scheduler loop failed to start (jobs will not dispatch)")
        else:
            self._scheduler_loop = None
            logger.info(
                "scheduler.execution_loop_disabled (FF_SCHEDULER_EXECUTION=off) — "
                "unattended job execution is gated off pending the offline-grant "
                "security review (030 FR-004/FR-005; 025 T057)"
            )

        # Feature 027 (click-through finding): user-created agents that went
        # live do not survive a restart — nothing relaunched them, leaving
        # "My agents" empty and the original requests unservable. Relaunch
        # every live generated agent without touching ownership or the user's
        # saved scopes (align_scopes=False).
        async def _relaunch_generated_agents():
            await asyncio.sleep(5)  # let the static-fleet monitor settle first
            try:
                rows = await self.history.db.afetch_all(LIVE_DRAFT_RELAUNCH_QUERY)
            except Exception:
                logger.exception("relaunch: could not list live generated agents")
                return
            for row in rows:
                try:
                    await self.lifecycle_manager.start_draft_agent(
                        row["id"], align_scopes=False)
                    logger.info("relaunch: %s (%s) restarted", row["agent_name"], row["id"])
                except Exception as exc:
                    logger.warning("relaunch: %s (%s) failed: %s",
                                   row["agent_name"], row["id"], exc)

        asyncio.create_task(_relaunch_generated_agents())

        # Import WebSocket protocol docs for OpenAPI description
        from orchestrator.models import WS_PROTOCOL_DOCS

        # OpenAPI tag metadata for grouping endpoints in /docs
        tags_metadata = [
            {"name": "Chat", "description": "Chat session management — create, list, load, delete chats and send messages."},
            {"name": "Components", "description": "Saved UI component management — save, list, delete, combine, and condense components."},
            {"name": "Agents", "description": "Connected agent discovery and information."},
            {"name": "System", "description": "System dashboard and configuration."},
            {"name": "Auth", "description": "Authentication token proxy (Keycloak BFF)."},
            {"name": "Files", "description": "File upload and download."},
            {"name": "Audit", "description": "Per-user audit log (HIPAA + NIST AU). Read-only; admin-blind."},
        ]

        # Create FastAPI app with rich OpenAPI documentation
        app = FastAPI(
            title="AstralDeep Orchestrator API",
            description=(
                "REST API and WebSocket gateway for the AstralDeep multi-agent system.\n\n"
                "## Overview\n\n"
                "The orchestrator provides two communication channels:\n\n"
                "1. **REST API** (documented below) — Request/response endpoints for CRUD operations\n"
                "2. **WebSocket** (`ws://<host>:<port>/ws`) — Real-time streaming for chat responses and live updates\n\n"
                "Both channels share the same authentication (Keycloak JWT Bearer tokens).\n\n"
                "---\n"
                + WS_PROTOCOL_DOCS
            ),
            version="1.0.0",
            openapi_tags=tags_metadata,
            docs_url="/api/docs",
            redoc_url="/api/redoc",
            openapi_url="/api/openapi.json",
        )

        # Constitution VI: interactive API docs MUST answer at the literal
        # /docs URL. The canonical pages stay /api-namespaced; these aliases
        # redirect (no second Swagger mount, no schema duplication).
        from fastapi.responses import RedirectResponse as _DocsRedirect

        @app.get("/docs", include_in_schema=False)
        async def _docs_alias():
            return _DocsRedirect("/api/docs")

        @app.get("/redoc", include_in_schema=False)
        async def _redoc_alias():
            return _DocsRedirect("/api/redoc")

        @app.get("/openapi.json", include_in_schema=False)
        async def _openapi_alias():
            return _DocsRedirect("/api/openapi.json")

        # CORS — the web UI is same-origin since feature 026 (the orchestrator
        # serves it), so cross-origin access is the exception, not the rule.
        # Default allowlist = this deployment's own public URLs; extend with
        # CORS_ORIGINS (comma-separated) for legitimate external consumers.
        # (The former :5173 React-dev defaults are gone with the SPA.)
        if os.getenv("CORS_ORIGINS"):
            cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
        else:
            cors_origins = sorted({
                o.rstrip("/") for o in (
                    os.getenv("PUBLIC_BASE_URL", ""),
                    os.getenv("BACKEND_PUBLIC_URL", ""),
                    f"http://localhost:{os.getenv('ORCHESTRATOR_PORT', '8001')}",
                ) if o
            })
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        # Store Orchestrator instance on app.state so REST API routes can access it
        app.state.orchestrator = self

        # Feature 065: mount the internal authenticated worker pool only when
        # fail-closed voice construction and its dedicated control secret both
        # passed. The installer is idempotent and rejects path collisions.
        try:
            from orchestrator.voice_bootstrap import install_voice_worker_control

            self.voice_worker_endpoint = install_voice_worker_control(
                app,
                self.voice_services,
            )
        except Exception as exc:
            self.voice_worker_endpoint = None
            logger.warning(
                "voice_worker_control_unavailable",
                extra={"reason": getattr(exc, "code", type(exc).__name__)},
            )

        # ── Health probes (ungated; no user data) ───────────────────────────
        # /healthz: liveness — the process is serving. /readyz: readiness —
        # the database answers. Wired into the compose healthcheck and any
        # orchestration platform (k8s livenessProbe/readinessProbe).
        @app.get("/healthz", include_in_schema=False)
        async def healthz():
            return {"status": "ok"}

        @app.get("/readyz", include_in_schema=False)
        async def readyz():
            from fastapi.responses import JSONResponse as _JSON
            try:
                row = await asyncio.to_thread(self.history.db.fetch_one, "SELECT 1 AS ok")
                if not row:
                    raise RuntimeError("empty health-probe result")
            except Exception as exc:
                logger.warning("readyz: database probe failed: %s", exc)
                return _JSON({"status": "degraded", "db": "unreachable"}, status_code=503)
            return {"status": "ok", "db": "ok", "agents": len(self.agent_cards)}

        @app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            await self.handle_ui_connection_fastapi(websocket)

        # ── Feature 026: serve the server-driven web UI from this app ──────
        # The shell page + static assets replace the former separate React SPA
        # (no separate :5173 frontend). astralprims defines primitives, the
        # orchestrator renders them (webrender), ROTE adapts per device.
        import os as _os
        from fastapi.responses import HTMLResponse as _HTMLResponse
        _webrender_dir = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "webrender")
        _shell_path = _os.path.join(_webrender_dir, "templates", "shell.html")

        @app.get("/", response_class=_HTMLResponse)
        async def serve_shell(request: Request):
            # Feature 028 (FR-001): the shell is gated. Unauthenticated
            # visitors are redirected straight to Keycloak via /auth/login
            # with their destination preserved — no app markup is served.
            try:
                from orchestrator.web_auth import shell_gate
                from fastapi.responses import RedirectResponse as _Redirect
                gate = await asyncio.to_thread(shell_gate, request)
                if gate:
                    return _Redirect(gate, status_code=302)
            except Exception:
                logger.exception("web_auth: shell gate check failed — failing closed")
                return _HTMLResponse("<h1>AstralDeep</h1><p>Sign-in unavailable.</p>", status_code=503)
            try:
                with open(_shell_path, "r", encoding="utf-8") as fh:
                    shell = fh.read()
            except Exception:
                logger.exception("webrender: shell template missing")
                return _HTMLResponse("<h1>AstralDeep</h1><p>UI shell unavailable.</p>", status_code=500)
            # Inject a session token for the WS handshake. In mock-auth/dev the
            # client falls back to 'dev-token'; with server-side OIDC the auth
            # routes establish a session and supply the access token here.
            token = ""
            try:
                token = await self._shell_token_for_request(request)
            except Exception:
                token = ""
            # Feature 027: render the static top bar + settings menu from the
            # server session's roles (admin group absent for non-admins —
            # FR-014 UX gating; handlers re-check server-side).
            topbar = ""
            try:
                from orchestrator.web_auth import session_roles
                from webrender.chrome import render_topbar
                topbar = render_topbar(roles=await asyncio.to_thread(session_roles, request))
            except Exception:
                logger.exception("chrome: topbar render failed — serving bare shell")
            shell = shell.replace("%%ASTRAL_TOKEN%%", token or "")
            # Feature 028 (FR-011): server-derived resume flag — false only on
            # the load right after interactive sign-in; the client echoes it
            # into register_ui so auth.session_resumed keeps 016 semantics.
            resumed_flag = "true"
            try:
                from orchestrator.web_auth import session_resumed_flag
                resumed_flag = "true" if await asyncio.to_thread(session_resumed_flag, request) else "false"
            except Exception:
                logger.debug("session_resumed_flag failed", exc_info=True)
            shell = shell.replace("%%ASTRAL_RESUMED%%", resumed_flag)
            # Feature 031: inject the file-input `accept` list from the server's
            # content_type allow-list so the picker offers exactly the accepted
            # extensions (single source of truth; server still validates uploads).
            accept_attr = ""
            try:
                from orchestrator.attachments.content_type import ACCEPTED_EXTENSIONS
                accept_attr = ",".join("." + e for e in sorted(ACCEPTED_EXTENSIONS))
            except Exception:
                logger.debug("attachment accept-list injection failed", exc_info=True)
            shell = shell.replace("%%ASTRAL_ACCEPT%%", accept_attr)
            # Feature 052: per-file content-hash asset URLs — a changed file is
            # fetched under a new URL, unchanged files stay immutable-cached.
            shell = _apply_asset_versions(shell, _os.path.join(_webrender_dir, "static"))
            resp = _HTMLResponse(shell.replace("%%ASTRAL_TOPBAR%%", topbar))
            # The shell carries a per-session token and references versioned
            # assets — never cache it (security + always-fresh asset URLs).
            resp.headers["Cache-Control"] = "no-store"
            return resp

        app.mount("/static", _NoCacheStaticFiles(directory=_os.path.join(_webrender_dir, "static")), name="static")

        # Mount REST API routers
        from orchestrator.api import chat_router, component_router, agent_router, dashboard_router, draft_router, voice_router, task_router, async_task_router, user_router, chrome_router, export_router, share_router, operation_router
        from orchestrator.auth import auth_router
        from orchestrator.web_auth import web_auth_router  # Feature 026 — server-side OIDC
        from orchestrator.attachments.router import attachments_router
        from audit.api import audit_router
        from audit.middleware import AuditHTTPMiddleware
        from feedback.api import feedback_user_router, feedback_admin_router
        from onboarding.api import onboarding_user_router, onboarding_admin_router
        from llm_config.api import llm_router  # Feature 006-user-llm-config
        # Feature 025 — agentic soul integration
        from personalization.api import (
            personalization_router,
            skills_router,
            memory_router,
        )
        from scheduler.api import schedule_router
        from dreaming.api import dreaming_router
        app.include_router(chat_router)
        app.include_router(component_router)
        app.include_router(agent_router)
        app.include_router(user_router)  # Feature 013 — tool-selection prefs
        app.include_router(draft_router)
        app.include_router(dashboard_router)
        app.include_router(chrome_router)  # Feature 042 — GET /api/chrome/menu
        # Feature 055 US5 — flag-gated export/share (routes 404 while off)
        app.include_router(export_router)
        app.include_router(share_router)
        app.include_router(auth_router)
        app.include_router(web_auth_router)  # Feature 026 — /auth/login,/callback,/session,/logout
        app.include_router(attachments_router)
        app.include_router(voice_router)
        app.include_router(task_router)
        app.include_router(async_task_router)
        app.include_router(operation_router)
        app.include_router(audit_router)
        # Feature 004 — component feedback & tool-improvement loop
        app.include_router(feedback_user_router)
        app.include_router(feedback_admin_router)
        # Feature 005 — tool tips and getting started tutorial
        app.include_router(onboarding_user_router)
        app.include_router(onboarding_admin_router)
        # Feature 006 — user-configurable LLM subscription (Test Connection)
        app.include_router(llm_router)
        # Feature 025 — agentic soul integration
        app.include_router(personalization_router)
        app.include_router(skills_router)
        app.include_router(memory_router)
        app.include_router(schedule_router)
        app.include_router(dreaming_router)

        # Audit HTTP middleware — records every authenticated REST request
        # in the caller's own log (FR-021). Added after CORS so OPTIONS
        # preflights are short-circuited before reaching the recorder.
        app.add_middleware(AuditHTTPMiddleware)

        # Feature 064: the MCP resource server is absent — routes, metadata,
        # renderer registration, and CORS policy included — unless the startup
        # flag was enabled. FeatureFlags is import-time state; recreate the
        # container to enable or disable this surface.
        if flags.is_enabled("mcp_server"):
            from orchestrator.mcp_server_endpoint import install_mcp_server

            install_mcp_server(app, self)
            logger.info("MCP 2026-07-28 endpoint mounted at /mcp")

        # Mount A2A JSON-RPC server (orchestrator as A2A agent)
        try:
            from orchestrator.a2a_orchestrator_executor import setup_orchestrator_a2a
            setup_orchestrator_a2a(app, self)
            logger.info("A2A JSON-RPC endpoint mounted at /a2a/")
        except Exception as e:
            logger.warning(f"A2A server setup skipped: {e}")

        # Discover external A2A agents from env var
        external_agents = os.getenv("A2A_EXTERNAL_AGENTS", "")
        if external_agents:
            async def _discover_external():
                await asyncio.sleep(3)  # Wait for server to start
                for url in external_agents.split(","):
                    url = url.strip()
                    if url:
                        await self.discover_a2a_agent(url)
            asyncio.create_task(_discover_external())

        # Start combined server. proxy_headers honors X-Forwarded-Proto/-For
        # from a TLS-terminating reverse proxy (production deployments) so
        # request.base_url is https — which drives the session cookie's
        # `secure` flag and the OIDC redirect_uri. Only proxies listed in
        # FORWARDED_ALLOW_IPS are trusted (default: loopback only).
        config = uvicorn.Config(
            app, host="0.0.0.0", port=PORT,
            log_level=os.getenv("LOG_LEVEL", "info").lower(),
            proxy_headers=True,
            forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        )
        server = uvicorn.Server(config)
        logger.info(f"Consolidated server (Gateway) listening on http://0.0.0.0:{PORT}")
        logger.info(f"API docs available at http://localhost:{PORT}/docs")
        logger.info(f"A2A endpoint: http://localhost:{PORT}/a2a/")
        retention_interval = float(
            os.getenv("OPERATION_RETENTION_SWEEP_SECONDS", "3300")
        )
        retention_retry = float(
            os.getenv("OPERATION_RETENTION_RETRY_SECONDS", "60")
        )
        self.async_task_manager.start_retention_sweep(
            interval_seconds=retention_interval,
            retry_seconds=retention_retry,
            on_sweep=self.task_manager.prune_missing,
        )
        if self.voice_services is not None:
            self.voice_services.start()
        try:
            await server.serve()
        finally:
            watchdog = getattr(self, "_personal_agent_watchdog_task", None)
            if watchdog is not None:
                watchdog.cancel()
                await asyncio.gather(watchdog, return_exceptions=True)
                self._personal_agent_watchdog_task = None
            poller = getattr(self, "_remote_job_poll_task", None)
            if poller is not None:
                poller.cancel()
                await asyncio.gather(poller, return_exceptions=True)
                self._remote_job_poll_task = None
            try:
                scheduler_loop = getattr(self, "_scheduler_loop", None)
                if scheduler_loop is not None:
                    await scheduler_loop.stop()
            finally:
                try:
                    remainder = await self.async_task_manager.drain(
                        timeout_seconds=5.0
                    )
                    if remainder:
                        logger.critical(
                            "Background service drain fenced %d "
                            "cancellation-resistant task(s)",
                            remainder,
                        )
                finally:
                    # Kept as an idempotent compatibility guard if a partial
                    # startup failed before drain captured the retention task.
                    try:
                        await self.async_task_manager.stop_retention_sweep()
                    finally:
                        voice_services = getattr(self, "voice_services", None)
                        if voice_services is not None:
                            try:
                                await voice_services.close()
                            except Exception:
                                logger.warning(
                                    "conversational_voice_shutdown_failed"
                                )

    async def _jwks_warm_loop(self):
        """Warm the Keycloak JWKS at boot, then refresh it in the background.

        Feature 052 (FR-011): the first fetch removes the cold IdP round trip
        from the interactive sign-in path; the periodic refetch (default 500 s,
        inside the cache's 600 s TTL) keeps it warm. IdP failures log and back
        off — boot, /readyz, and the fail-closed validation path are untouched
        (a failed fetch never caches anything). Skipped cleanly under mock
        auth or when no authority is configured.
        """
        from shared import jwks_cache
        authority = (os.getenv("KEYCLOAK_AUTHORITY") or "").strip()
        if not authority or os.getenv("USE_MOCK_AUTH", "").lower() == "true":
            logger.info("jwks warm: skipped (mock auth or no authority configured)")
            return
        jwks_url = f"{authority.rstrip('/')}/protocol/openid-connect/certs"
        interval = float(os.getenv("JWKS_REFRESH_SECONDS", "500"))
        backoff = 5.0
        warmed = False
        while True:
            try:
                if warmed:
                    # _fetch bypasses the TTL — get_jwks would no-op inside
                    # its 600 s window and let the cache go cold at expiry.
                    await jwks_cache._fetch(jwks_url)
                else:
                    with perf_span("boot.jwks_warm"):
                        await jwks_cache.get_jwks(jwks_url)
                    warmed = True
                    logger.info("jwks warm: keys cached for %s", jwks_url)
                backoff = 5.0
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("jwks warm/refresh failed (%s) — retrying in %.0fs",
                               exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300.0)

    def _start_phi_warm(self):
        """Spawn a daemon thread that builds the PHI analyzer singleton.

        Feature 052 (FR-028): the Presidio+spaCy load happens in the
        background instead of stalling the first interactive use. Readiness
        is independent; a first request racing the warm-up just blocks on the
        singleton exactly as before. ``FF_PHI_WARM=false`` disables the
        pre-warm (lazy first-use semantics are then unchanged).
        """
        if os.getenv("FF_PHI_WARM", "true").lower() not in ("true", "1", "yes"):
            logger.info("phi warm: disabled via FF_PHI_WARM")
            return

        def _phi_warm_worker():
            """Build the PHI gate singleton off the boot path."""
            try:
                from personalization.phi_gate import get_phi_gate
                with perf_span("boot.phi_warm"):
                    get_phi_gate()
                logger.info("phi warm: analyzer ready")
            except Exception:
                logger.debug("phi warm-up failed (non-fatal)", exc_info=True)

        import threading
        threading.Thread(target=_phi_warm_worker, name="phi-warm", daemon=True).start()

    async def _monitor_agents(self, start_port: int, max_ports: int = 10):
        """Continuously monitor and discover agents across a range of ports."""
        logger.info(f"Starting agent monitor for ports {start_port} to {start_port + max_ports - 1}...")

        while True:
            for port in range(start_port, start_port + max_ports):
                agent_url = f"http://localhost:{port}"
                try:
                    # This will connect if not already connected
                    await self.discover_agent(agent_url)
                except Exception:
                    pass
            
            await asyncio.sleep(5)  # Check every 5 seconds

    async def summarize_chat_title(self, chat_id: str, message: str, user_id: str = 'legacy', websocket=None):
        """Generate a concise title for the chat using LLM.

        Feature 006: routes through the per-user / operator-default
        credential resolver. If the user has personal credentials
        configured, those are used; otherwise the operator default.
        ``LLMUnavailable`` (no credentials anywhere) returns silently —
        a missing chat title is non-fatal.
        """
        feature = "chat_title"
        actor_user_id, auth_principal = self._llm_audit_principals(websocket)
        try:
            client, source, resolved = await self._resolve_llm_client_for(websocket)
        except self._LLMUnavailable:
            await self._record_llm_unconfigured(
                self.audit_recorder,
                actor_user_id=actor_user_id,
                auth_principal=auth_principal,
                feature=feature,
            )
            return

        try:
            response = await asyncio.to_thread(
                client.chat.completions.create,
                model=resolved.model,
                messages=[
                    {"role": "system", "content": "Summarize the following user request into a concise 3-5 word title. Return ONLY the title, no quotes or other text."},
                    {"role": "user", "content": message}
                ],
                max_tokens=20
            )
            usage = getattr(response, "usage", None)
            total_tokens = getattr(usage, "total_tokens", None) if usage else None
            await self._record_llm_call(
                self.audit_recorder,
                actor_user_id=actor_user_id,
                auth_principal=auth_principal,
                feature=feature,
                credential_source=source,
                resolved=resolved,
                total_tokens=total_tokens,
                outcome="success",
            )
            if source == self._CredentialSource.USER and websocket is not None:
                await self._emit_llm_usage_report(
                    websocket, feature=feature, model=resolved.model,
                    usage=usage, outcome="success",
                )
            content = strip_reasoning_markup(response.choices[0].message.content)
            if not content:
                return
            title = content.strip().strip('"')

            # Update history and notify UI
            self.history.update_chat_title(chat_id, title, user_id=user_id)

            # Broadcast update (each user gets their own history)
            await self._broadcast_user_history()

        except Exception as e:
            error = self._safe_llm_error_metadata(e)
            logger.error(
                "Chat-title LLM call failed exception_class=%s "
                "status_code=%s upstream_error_class=%s",
                error.exception_class,
                error.status_code,
                error.upstream_error_class,
            )
            await self._record_llm_call(
                self.audit_recorder,
                actor_user_id=actor_user_id,
                auth_principal=auth_principal,
                feature=feature,
                credential_source=source,
                resolved=resolved,
                total_tokens=None,
                outcome="failure",
                upstream_error_class=error.upstream_error_class,
            )
            if source == self._CredentialSource.USER and websocket is not None:
                await self._emit_llm_usage_report(
                    websocket, feature=feature, model=resolved.model,
                    usage=None, outcome="failure",
                )

    # =========================================================================
    # AUTHENTICATION
    # =========================================================================

    async def validate_token(self, token: str) -> Optional[Dict]:
        """Validate JWT token against KeyCloak."""
        if os.getenv("USE_MOCK_AUTH", "").lower() == "true":
            if token == "dev-token":
                logger.info("Mock Auth: Validated dev-token as test_user")
                return {
                    "sub": "test_user",
                    "preferred_username": "test_user",
                    "email": "test_user@local",
                    "realm_access": {"roles": ["admin", "user"]},
                    "resource_access": {"astral-frontend": {"roles": ["admin", "user"]}},
                }
            try:
                import base64
                parts = token.split('.')
                if len(parts) == 3:
                    payload_b64 = parts[1]
                    payload_b64 += '=' * ((4 - len(payload_b64) % 4) % 4)
                    payload_json = base64.b64decode(payload_b64).decode('utf-8')
                    return json.loads(payload_json)
            except Exception as e:
                logger.debug(f"Mock JWT decode failed, falling back to default test_user: {e}")
            logger.info("Mock Auth: Accepting token as test_user")
            return {
                "sub": "test_user",
                "preferred_username": "test_user",
                "email": "test_user@local",
                "realm_access": {"roles": ["admin", "user"]},
                "resource_access": {"astral-frontend": {"roles": ["admin", "user"]}},
            }

        try:
            authority = os.getenv("KEYCLOAK_AUTHORITY")
            expected_client = os.getenv("KEYCLOAK_CLIENT_ID")
            
            if not authority or not expected_client:
                logger.warning("Auth not configured (KEYCLOAK_AUTHORITY/CLIENT_ID missing)")
                return None

            # Fetch JWKS (feature 028 D8: cached with kid-miss refetch — the
            # pre-028 per-call fetch made every WS register an IdP round-trip)
            jwks_url = f"{authority}/protocol/openid-connect/certs"
            from shared.jwks_cache import get_jwks
            jwks = await get_jwks(jwks_url, token=token)

            # Verify token — skip strict audience check since Keycloak
            # confidential clients set aud="account", not the client_id.
            # We validate azp (authorized party) instead.
            payload = jose_jwt.decode(
                token,
                jwks,
                algorithms=["RS256"],
                options={"verify_aud": False, "verify_at_hash": False}
            )

            # Bind the token to our realm: reject when the issuer claim is
            # present and does not match the configured authority (defense
            # beyond the signature alone; tolerant of a trailing-slash diff).
            iss = payload.get("iss")
            if iss and authority and iss.rstrip("/") != authority.rstrip("/"):
                logger.warning("Token 'iss' does not match the configured authority — rejecting")
                return None

            # Verify authorized party is an accepted client. The web client
            # (expected_client) is always accepted; additional first-party
            # clients — notably the native desktop's dedicated public client
            # astral-desktop — are accepted via the KEYCLOAK_ALLOWED_AZP
            # allow-list (RFC 8252 native-app posture). Empty allow-list ⇒
            # only the web client (identical to the legacy single-azp check).
            azp = payload.get("azp")
            from shared.auth_clients import is_azp_allowed, allowed_azps
            if azp and not is_azp_allowed(azp):
                logger.warning(
                    f"Token azp '{azp}' is not an accepted client "
                    f"(allowed: {sorted(allowed_azps())})"
                )
                return None

            # Extract Roles
            client_id = os.getenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
            roles = payload.get("realm_access", {}).get("roles", [])
            if "resource_access" in payload:
                if client_id in payload["resource_access"]:
                    client_roles = payload["resource_access"][client_id].get("roles", [])
                    roles.extend(client_roles)
                if "account" in payload["resource_access"]:
                    account_roles = payload["resource_access"]["account"].get("roles", [])
                    roles.extend(account_roles)
            
            logger.debug(f"Token validation: extracted roles {roles} from payload keys {list(payload.keys())}")
            
            if "admin" not in roles and "user" not in roles:
                logger.warning(f"Token unauthorized (Requires 'admin' or 'user' role). Found roles: {roles}")
                return None

            return payload
        except Exception as e:
            logger.error(f"Token validation failed: {e}")
            return None

    def _get_user_id(self, websocket) -> str:
        """Extract user_id from the UI session, default to 'legacy' if not authenticated."""
        if websocket not in self.ui_sessions:
            return 'legacy'
        user_data = self.ui_sessions[websocket]
        # user_data is the JWT payload, sub is the subject (user ID)
        return user_data.get('sub', 'legacy')

    def _save_user_profile(self, user_data: Dict) -> None:
        """Persist user profile from JWT claims to the database."""
        user_id = user_data.get("sub")
        if not user_id or user_id == "legacy":
            return
        try:
            # Extract roles from JWT claims
            roles = list(set(
                user_data.get("realm_access", {}).get("roles", []) +
                user_data.get("resource_access", {}).get(
                    os.getenv("KEYCLOAK_CLIENT_ID", "astral-frontend"), {}
                ).get("roles", [])
            ))
            self.history.db.upsert_user(
                user_id=user_id,
                email=user_data.get("email"),
                username=user_data.get("preferred_username"),
                display_name=user_data.get("name") or user_data.get("preferred_username"),
                roles=roles,
            )
        except Exception as e:
            logger.warning(f"Failed to save user profile for {user_id}: {e}")


if __name__ == "__main__":
    orch = Orchestrator()
    asyncio.run(orch.start())
