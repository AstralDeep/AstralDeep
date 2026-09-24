"""Recording-site helpers wrapping AuditEventCreate construction for the orchestrator
and other backend modules (auth, WebSocket actions, workspace/share lifecycle, tool
dispatch); no-ops when no Recorder is wired.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .recorder import get_recorder, make_correlation_id, now_utc
from .schemas import AuditEventCreate, ArtifactPointer

logger = logging.getLogger("Audit.Hooks")

def actor_principal_from_claims(claims: Optional[Dict[str, Any]]) -> tuple[str, str]:
    if not claims:
        return "legacy", "legacy"
    machine_class = claims.get("machine_class")
    if machine_class:
        owner = claims.get("sub")
        if owner:
            return owner, f"machine:{machine_class}"
    user = claims.get("sub", "legacy")
    act = claims.get("act") or {}
    principal = act.get("sub") if isinstance(act, dict) and act.get("sub") else user
    return user, principal


async def record_auth_event(
    *, claims: Dict[str, Any], action: str, description: str,
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


_NOISY_WS_ACTIONS = frozenset({
    "ping", "heartbeat",
})


async def record_ws_action(
    *, claims: Optional[Dict[str, Any]], action: str,
    chat_id: Optional[str] = None, payload: Optional[Dict[str, Any]] = None,
    outcome: str = "success", outcome_detail: Optional[str] = None,
) -> None:
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


async def record_workspace_event(
    *, user_id: str, action: str, chat_id: Optional[str] = None,
    component_id: Optional[str] = None, description: str = "",
    outcome: str = "success", detail: Optional[Dict[str, Any]] = None,
) -> None:
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


async def record_share_event(
    *, user_id: str, action: str, share_id: Optional[int] = None,
    chat_id: Optional[str] = None, description: str = "",
    outcome: str = "success", principal: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
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


class ToolDispatchAudit:
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
        self._correlation_id = correlation_id or make_correlation_id()
        self._started_at = now_utc()
        self._args_meta = self._sanitize_args_meta(args_meta or {})
        if invocation_channel:
            self._args_meta["invocation_channel"] = str(invocation_channel)[:32]
        if claims and claims.get("machine_class") and claims.get("consent_ref"):
            self._args_meta["consent_ref"] = str(claims["consent_ref"])
        self._outcome = "success"
        self._outcome_detail: Optional[str] = None
        self._outputs_meta: Dict[str, Any] = {}

    @staticmethod
    def _sanitize_args_meta(args: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        keys = []
        for k, v in args.items():
            if isinstance(k, str) and k.startswith("_"):
                # Underscore-prefixed keys (tokens/credentials) never get logged
                continue
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
        return self._correlation_id

    @property
    def actor_user_id(self) -> str:
        return actor_principal_from_claims(self._claims)[0]

    @property
    def auth_principal(self) -> str:
        return actor_principal_from_claims(self._claims)[1]

    @property
    def conversation_id(self) -> Optional[str]:
        return self._chat_id

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
