"""
Recording-site helpers used by the orchestrator and other backend modules.

Each helper wraps the construction of an ``AuditEventCreate`` so the
call sites stay short and consistent. The helpers extract the
``actor_user_id`` and ``auth_principal`` (RFC 8693 actor claim, per
research.md §R7) from the JWT payload — for direct user actions the
two values are equal; for agent actions the principal is the agent's
machine identity while ``actor_user_id`` is the on-behalf-of user.

All functions are no-ops when no ``Recorder`` is wired (returns
``None``) so unit tests and other call paths that don't need audit
plumbing pay no overhead.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .recorder import get_recorder, make_correlation_id, now_utc
from .schemas import AuditEventCreate, ArtifactPointer

logger = logging.getLogger("Audit.Hooks")

# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------

def actor_principal_from_claims(claims: Optional[Dict[str, Any]]) -> tuple[str, str]:
    """Return ``(actor_user_id, auth_principal)`` from JWT claims.

    For RFC 8693 delegated tokens, ``act.sub`` carries the *delegating*
    party (the agent) and ``sub`` carries the user; for normal user
    tokens both are equal. We treat ``sub`` as the on-behalf-of user
    (``actor_user_id``) and ``auth_principal`` as the token's *acting*
    subject (the agent for delegated calls, otherwise the user).
    """
    if not claims:
        return "legacy", "legacy"
    # 056 FR-014: machine-initiated turns (scheduled runs, parser replay,
    # draft self-tests) carry a synthetic machine-context claims dict — set by
    # MachineTurnAuthority on the turn's virtual socket — instead of a session
    # JWT. Resolve it BEFORE the legacy fallback so machine-turn records are
    # recorded and attributed to machine:<class> acting for the owning human,
    # never dropped as "legacy"/"unknown" (SC-005).
    machine_class = claims.get("machine_class")
    if machine_class:
        owner = claims.get("sub")
        if owner:
            return owner, f"machine:{machine_class}"
    user = claims.get("sub", "legacy")
    act = claims.get("act") or {}
    principal = act.get("sub") if isinstance(act, dict) and act.get("sub") else user
    return user, principal


# ---------------------------------------------------------------------------
# Auth lifecycle (FR-001 + AU-2)
# ---------------------------------------------------------------------------

async def record_auth_event(
    *, claims: Dict[str, Any], action: str, description: str,
    outcome: str = "success", outcome_detail: Optional[str] = None,
) -> None:
    """Record a login / logout / token-refresh event."""
    rec = get_recorder()
    if rec is None:
        return
    user, principal = actor_principal_from_claims(claims)
    if user == "legacy":
        return  # don't record unauthenticated noise
    try:
        await rec.record(AuditEventCreate(
            actor_user_id=user,
            auth_principal=principal,
            event_class="auth",
            action_type=f"auth.{action}",
            description=description,
            correlation_id=make_correlation_id(),
            outcome=outcome,
            outcome_detail=outcome_detail,
            inputs_meta={
                "preferred_username": claims.get("preferred_username"),
                "azp": claims.get("azp"),
            },
            started_at=now_utc(),
        ))
    except Exception as exc:  # pragma: no cover
        logger.debug("auth audit record failed: %s", exc)


# ---------------------------------------------------------------------------
# WebSocket message handler hooks (FR-001 + AU-2)
# ---------------------------------------------------------------------------

_NOISY_WS_ACTIONS = frozenset({
    # Re-render-style messages with no state change. We do not record
    # these to keep the log readable. The component-class allowlist
    # below covers state-changing renders separately.
    "ping", "heartbeat",
})


async def record_ws_action(
    *, claims: Optional[Dict[str, Any]], action: str,
    chat_id: Optional[str] = None, payload: Optional[Dict[str, Any]] = None,
    outcome: str = "success", outcome_detail: Optional[str] = None,
) -> None:
    """Record a WebSocket UI action (e.g. ``chat_message``, ``load_chat``)."""
    if not action or action in _NOISY_WS_ACTIONS:
        return
    rec = get_recorder()
    if rec is None:
        return
    user, principal = actor_principal_from_claims(claims)
    if user == "legacy":
        return
    inputs_meta: Dict[str, Any] = {"action": action}
    if payload:
        # Carry only sizes / shape, never the raw user message body.
        for safe_key in ("draft_agent_id", "tool_name", "agent_id", "url"):
            if safe_key in payload and isinstance(payload[safe_key], (str, int, float, bool)):
                inputs_meta[safe_key] = payload[safe_key]
        if "message" in payload and isinstance(payload["message"], str):
            inputs_meta["message_length"] = len(payload["message"])
    try:
        await rec.record(AuditEventCreate(
            actor_user_id=user,
            auth_principal=principal,
            event_class="conversation" if action.startswith("chat_") or action in ("load_chat", "delete_chat", "save_component", "delete_component") else "settings",
            action_type=f"ws.{action}",
            description=f"User WS action {action!r}",
            conversation_id=chat_id,
            correlation_id=make_correlation_id(),
            outcome=outcome,
            outcome_detail=outcome_detail,
            inputs_meta=inputs_meta,
            started_at=now_utc(),
        ))
    except Exception as exc:  # pragma: no cover
        logger.debug("ws action audit record failed: %s", exc)


# ---------------------------------------------------------------------------
# Workspace lifecycle (feature 028 — FR-023/FR-033/FR-036)
# ---------------------------------------------------------------------------

async def record_workspace_event(
    *, user_id: str, action: str, chat_id: Optional[str] = None,
    component_id: Optional[str] = None, description: str = "",
    outcome: str = "success", detail: Optional[Dict[str, Any]] = None,
) -> None:
    """Record a workspace mutation / timeline view / denied component action.

    ``action`` is the suffix after ``workspace.`` — e.g. ``component_added``,
    ``component_updated``, ``component_removed``, ``action_denied``,
    ``timeline_viewed``. Classified under ``conversation`` to match the
    existing save/delete-component WS hook classification.
    """
    rec = get_recorder()
    if rec is None or not user_id or user_id == "legacy":
        return
    inputs_meta: Dict[str, Any] = {}
    if component_id:
        inputs_meta["component_id"] = component_id
    for k, v in (detail or {}).items():
        if isinstance(v, (str, int, float, bool)):
            inputs_meta[k] = v
    try:
        await rec.record(AuditEventCreate(
            actor_user_id=user_id,
            auth_principal=user_id,
            event_class="conversation",
            action_type=f"workspace.{action}",
            description=description or f"Workspace {action.replace('_', ' ')}",
            conversation_id=chat_id,
            correlation_id=make_correlation_id(),
            outcome=outcome,
            inputs_meta=inputs_meta,
            started_at=now_utc(),
        ))
    except Exception as exc:  # pragma: no cover
        logger.debug("workspace audit record failed: %s", exc)


# ---------------------------------------------------------------------------
# Share-grant lifecycle (feature 055 US5 — research D11)
# ---------------------------------------------------------------------------

async def record_share_event(
    *, user_id: str, action: str, share_id: Optional[int] = None,
    chat_id: Optional[str] = None, description: str = "",
    outcome: str = "success", principal: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    """Record a share-grant lifecycle event.

    ``action`` is the suffix after ``share.`` — ``minted``, ``opened``,
    ``revoked``, ``refused_phi``. Classified under ``conversation`` per
    data-model.md, matching the workspace hooks. ``principal`` overrides
    ``auth_principal`` for public opens (``share:<id>`` — the actor stays
    the share owner). Rows carry ids and scalar metadata only — never token
    material or snapshot content.
    """
    rec = get_recorder()
    if rec is None or not user_id or user_id == "legacy":
        return
    inputs_meta: Dict[str, Any] = {}
    if share_id is not None:
        inputs_meta["share_id"] = share_id
    for k, v in (detail or {}).items():
        if isinstance(v, (str, int, float, bool)):
            inputs_meta[k] = v
    try:
        await rec.record(AuditEventCreate(
            actor_user_id=user_id,
            auth_principal=principal or user_id,
            event_class="conversation",
            action_type=f"share.{action}",
            description=description or f"Share {action.replace('_', ' ')}",
            conversation_id=chat_id,
            correlation_id=make_correlation_id(),
            outcome=outcome,
            inputs_meta=inputs_meta,
            started_at=now_utc(),
        ))
    except Exception as exc:  # pragma: no cover
        logger.debug("share audit record failed: %s", exc)


# ---------------------------------------------------------------------------
# Tool-dispatch hook (FR-001 + FR-021 — the headline scenario)
# ---------------------------------------------------------------------------

class ToolDispatchAudit:
    """Context-manager-style helper around a tool call.

    Use::

        async with ToolDispatchAudit(claims, agent_id, tool_name, chat_id) as audit:
            result = await dispatch(...)
            audit.set_result(result)
    """

    def __init__(
        self, *, claims: Optional[Dict[str, Any]], agent_id: Optional[str],
        tool_name: str, chat_id: Optional[str],
        args_meta: Optional[Dict[str, Any]] = None,
        correlation_id: Optional[str] = None,
        invocation_channel: Optional[str] = None,
    ):
        self._claims = claims
        self._agent_id = agent_id
        self._tool_name = tool_name
        self._chat_id = chat_id
        # 056 US1: a chained hop passes its delegation.hop.mint/.enforce
        # correlation id here so the hop's tool.start/end pair shares it —
        # the whole hop reconstructs from one id (SC-003).
        self._correlation_id = correlation_id or make_correlation_id()
        self._started_at = now_utc()
        self._args_meta = self._sanitize_args_meta(args_meta or {})
        if invocation_channel:
            self._args_meta["invocation_channel"] = str(invocation_channel)[:32]
        # 056 FR-014: machine-turn records carry the run's authorizing consent
        # reference so authority is attributable from the row alone — while
        # cost attribution stays on the system LLM credential (054), the two
        # never blur.
        if claims and claims.get("machine_class") and claims.get("consent_ref"):
            self._args_meta["consent_ref"] = str(claims["consent_ref"])
        self._outcome = "success"
        self._outcome_detail: Optional[str] = None
        self._outputs_meta: Dict[str, Any] = {}

    @staticmethod
    def _sanitize_args_meta(args: Dict[str, Any]) -> Dict[str, Any]:
        """Reduce tool args to non-PHI metadata.

        We record the names of present keys, the type of each value,
        and lengths for strings/lists/dicts. We never record the actual
        values — those are the user's data.
        """
        out: Dict[str, Any] = {}
        keys = []
        for k, v in args.items():
            if isinstance(k, str) and k.startswith("_"):
                continue  # internal injection (delegation token, credentials)
            keys.append(k)
            if isinstance(v, str):
                out[f"{k}_len"] = len(v)
            elif isinstance(v, (list, tuple)):
                out[f"{k}_count"] = len(v)
            elif isinstance(v, dict):
                out[f"{k}_keys"] = len(v)
        if keys:
            out["arg_keys"] = sorted(keys)[:16]
        return out

    def set_outcome(self, outcome: str, detail: Optional[str] = None) -> None:
        self._outcome = outcome
        self._outcome_detail = detail

    def set_outputs_meta(self, meta: Dict[str, Any]) -> None:
        self._outputs_meta = meta

    @property
    def correlation_id(self) -> str:
        """Public accessor for the per-dispatch correlation id.

        Feature 004 propagates this id onto the MCPResponse so each rendered
        component can be tagged with the originating dispatch's audit id —
        which the frontend's component_feedback flow uses to scope a user's
        feedback submission.
        """
        return self._correlation_id

    async def __aenter__(self) -> "ToolDispatchAudit":
        rec = get_recorder()
        user, principal = actor_principal_from_claims(self._claims)
        if rec is None or user == "legacy":
            return self
        try:
            await rec.record(AuditEventCreate(
                actor_user_id=user,
                auth_principal=principal,
                agent_id=self._agent_id,
                event_class="agent_tool_call",
                action_type=f"tool.{self._tool_name}.start",
                description=f"Agent {self._agent_id or '?'} dispatched tool {self._tool_name}",
                conversation_id=self._chat_id,
                correlation_id=self._correlation_id,
                outcome="in_progress",
                inputs_meta=self._args_meta,
                started_at=self._started_at,
            ))
        except Exception as exc:  # pragma: no cover
            logger.debug("tool start audit record failed: %s", exc)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        rec = get_recorder()
        user, principal = actor_principal_from_claims(self._claims)
        if rec is None or user == "legacy":
            return
        outcome = self._outcome
        detail = self._outcome_detail
        if exc is not None:
            outcome = "failure"
            detail = f"{exc.__class__.__name__}: {exc}"[:2000]
        try:
            await rec.record(AuditEventCreate(
                actor_user_id=user,
                auth_principal=principal,
                agent_id=self._agent_id,
                event_class="agent_tool_call",
                action_type=f"tool.{self._tool_name}.end",
                description=f"Tool {self._tool_name} completed ({outcome})",
                conversation_id=self._chat_id,
                correlation_id=self._correlation_id,
                outcome=outcome,
                outcome_detail=detail,
                inputs_meta=self._args_meta,
                outputs_meta=self._outputs_meta,
                started_at=self._started_at,
                completed_at=now_utc(),
            ))
        except Exception as exc:  # pragma: no cover
            logger.debug("tool end audit record failed: %s", exc)


# ---------------------------------------------------------------------------
# Generic file/conversation/settings recording (used by attachments etc.)
# ---------------------------------------------------------------------------

async def record_generic(
    *, claims: Optional[Dict[str, Any]], event_class: str, action_type: str,
    description: str, chat_id: Optional[str] = None,
    inputs_meta: Optional[Dict[str, Any]] = None,
    outputs_meta: Optional[Dict[str, Any]] = None,
    artifact_pointers: Optional[List[ArtifactPointer]] = None,
    outcome: str = "success", outcome_detail: Optional[str] = None,
) -> None:
    rec = get_recorder()
    if rec is None:
        return
    user, principal = actor_principal_from_claims(claims)
    if user == "legacy":
        return
    try:
        await rec.record(AuditEventCreate(
            actor_user_id=user,
            auth_principal=principal,
            event_class=event_class,
            action_type=action_type,
            description=description,
            conversation_id=chat_id,
            correlation_id=make_correlation_id(),
            outcome=outcome,
            outcome_detail=outcome_detail,
            inputs_meta=inputs_meta or {},
            outputs_meta=outputs_meta or {},
            artifact_pointers=artifact_pointers or [],
            started_at=now_utc(),
        ))
    except Exception as exc:  # pragma: no cover
        logger.debug("generic audit record failed: %s", exc)
