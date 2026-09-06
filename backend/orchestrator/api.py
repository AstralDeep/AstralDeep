"""
REST API routes for the AstralDeep backend.

Provides HTTP endpoints that mirror the WebSocket actions, enabling
any frontend (PHP, Flutter, other JS frameworks) to interact with
the orchestrator without implementing the WebSocket protocol.

WebSocket remains the primary channel for real-time features (streaming
chat responses, live status updates). These REST endpoints provide
request/response access for CRUD operations.
"""
import asyncio
import csv
import io
import json
import re
import time
import logging
import uuid
from datetime import UTC, date, datetime
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from orchestrator.models import (
    ChatMessageRequest, ChatMessageResponse,
    ChatListResponse, ChatSummary,
    ChatCreateRequest, ChatCreateResponse,
    ChatDetailResponse, ChatDetail,
    DeleteResponse,
    ComponentSaveRequest, ComponentSaveResponse, SavedComponent,
    ComponentListResponse,
    ComponentCombineRequest, ComponentCondenseRequest, ComponentCombineResponse,
    AgentListResponse, AgentInfo, AgentTool,
    AgentPermissionsRequest, AgentPermissionsResponse,
    AgentVisibilityRequest,
    ToolSelectionResponse, ToolSelectionUpdate,
    AgentEnabledUpdate, AgentEnabledResponse,
    CredentialSetRequest, CredentialListResponse, CredentialDeleteResponse,
    DashboardResponse,
    ErrorResponse,
    DraftAgentCreateRequest, DraftAgentRefineRequest, AdminReviewRequest,
    DraftAgentResponse, DraftAgentListResponse,
)
from orchestrator.auth import (
    get_current_user_payload,
    require_user_id,
    require_user_id_or_web_session,
    verify_admin,
)
from shared.feature_flags import flags
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionConfigurationError,
    OperationNotFoundError,
    OperationOwner,
    OwnerScope,
    SafeOperationProjection,
)
from orchestrator.voice_api import router as _voice_control_router

voice_router = _voice_control_router

logger = logging.getLogger("API")

# =============================================================================
# Helpers
# =============================================================================

def _get_orchestrator(request: Request):
    """Retrieve the shared Orchestrator instance from app state."""
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        # Walk up to parent app if mounted as sub-app
        root_app = getattr(request.app, "_root_app", None) or request.app
        orch = getattr(root_app.state, "orchestrator", None)
    if orch is None:
        raise HTTPException(status_code=503, detail="Orchestrator not initialized")
    return orch


def _plane_boundary(orch):
    """Return the one application-scoped Plane runtime and repository catalog."""

    composition = getattr(orch, "runtime_composition", None)
    plane = getattr(composition, "plane", None)
    runtime = getattr(plane, "runtime", None)
    repositories = getattr(plane, "repositories", None)
    if runtime is None or repositories is None:
        raise HTTPException(
            status_code=503,
            detail="AstralPlane runtime is not initialized",
        )
    return runtime, repositories


def _request_dispatch_identity(
    request: Request,
    user_id: str,
) -> tuple[Dict[str, Any], Optional[str]]:
    """Return only transport-verified REST identity for internal dispatch.

    Authentication dependencies store claims and the short-lived subject
    token on ``request.state``.  Keeping the bearer separate prevents audit
    serialization while still allowing the normal RFC 8693 gate to run.
    """

    state = getattr(request, "state", None)
    claims = getattr(state, "audit_claims", None)
    if not isinstance(claims, dict):
        # Direct unit invocations do not execute FastAPI dependencies.  Their
        # explicit ``user_id`` remains the already-resolved dependency value;
        # production requests always take the state-backed branch above.
        claims = {"sub": user_id}
    if claims.get("sub") != user_id:
        raise HTTPException(status_code=401, detail="Invalid dispatch identity")
    subject_token = getattr(state, "delegation_subject_token", None)
    if not isinstance(subject_token, str) or not subject_token:
        subject_token = None
    return dict(claims), subject_token


async def _run_atomic_canvas_mutation(
    orchestrator,
    *,
    chat_id: str,
    user_id: str,
    mutation,
):
    """Use the production publication boundary while keeping narrow test fakes usable."""

    runner = getattr(
        type(orchestrator), "run_detached_conversation_mutation", None
    )
    if runner is None:
        # Endpoint unit tests intentionally use a minimal SimpleNamespace or
        # MagicMock. Exercise the same durable repository boundary without
        # requiring the entire socket/delivery surface on that fake.
        from orchestrator.conversation_publication import (
            ConversationPublicationStage,
            activate_conversation_publication,
            reset_conversation_publication,
        )
        from orchestrator.history import ConversationCommitRepository

        repository = ConversationCommitRepository(
            plane_runtime=orchestrator.history.plane_runtime,
            plane_repositories=orchestrator.history.plane_repositories,
        )
        request_generation = str(uuid.uuid4())
        layouts = await orchestrator.workspace.alive_layouts(chat_id, user_id)
        staged = await asyncio.to_thread(
            repository.stage_commit,
            chat_id=chat_id,
            owner_user_id=user_id,
            request_generation=request_generation,
        )
        await asyncio.to_thread(
            repository.prepare_canvas_stage,
            commit_id=staged["commit_id"],
            owner_user_id=user_id,
        )
        stage = ConversationPublicationStage(
            history=orchestrator.history,
            commit_id=staged["commit_id"],
            chat_id=chat_id,
            user_id=user_id,
            base_render_revision=staged["base_render_revision"],
            next_render_revision=staged["base_render_revision"] + 1,
            layouts=layouts,
        )
        token = activate_conversation_publication(stage)
        try:
            result = await mutation()
            if not stage.dirty:
                raise RuntimeError(
                    "conversation mutation completed without a state change"
                )
            canvas = await orchestrator.workspace.alive_components(
                chat_id, user_id
            )
            staged_layouts = await orchestrator.workspace.alive_layouts(
                chat_id, user_id
            )
            await asyncio.to_thread(
                repository.publish_commit,
                commit_id=stage.commit_id,
                owner_user_id=user_id,
                messages=None,
                canvas_components=canvas,
                canvas_layouts=staged_layouts,
            )
            stage.seal(committed=True)
            if stage.snapshot_cause:
                await orchestrator.workspace.asnapshot(
                    chat_id, user_id, cause=stage.snapshot_cause
                )
            return result
        finally:
            if not stage.sealed:
                await asyncio.to_thread(
                    repository.abort_commit,
                    commit_id=stage.commit_id,
                    owner_user_id=user_id,
                )
                stage.seal(committed=False)
            reset_conversation_publication(token)
    return await runner(
        orchestrator,
        chat_id=chat_id,
        user_id=user_id,
        mutation=mutation,
    )


async def _workspace_identity_for_saved_row(orchestrator, **kwargs):
    from orchestrator.orchestrator import Orchestrator

    return await Orchestrator._workspace_identity_for_saved_row(
        orchestrator, **kwargs
    )


async def _replace_workspace_components(orchestrator, **kwargs):
    from orchestrator.orchestrator import Orchestrator

    return await Orchestrator._replace_workspace_components(
        orchestrator, **kwargs
    )


async def _refresh_saved_component_rows(
    orchestrator,
    chat_id: str,
    rows: List[Dict[str, Any]],
    ops: List[Dict[str, Any]],
    user_id: str,
) -> List[Dict[str, Any]]:
    """Replace staged physical row ids with the authoritative published ids."""

    refreshed = []
    for row, op in zip(rows, ops):
        current = await orchestrator.workspace.aget_by_component_id(
            chat_id, user_id, op["component_id"]
        )
        if current is None:
            raise HTTPException(
                status_code=500, detail="Published component unavailable"
            )
        item = dict(row)
        item["id"] = current["id"]
        item["component_data"] = current["component_data"]
        item["created_at"] = current.get("created_at") or item.get("created_at")
        refreshed.append(item)
    return refreshed


# =============================================================================
# Durable Operation Reconciliation Router (Feature 060 / T030)
# =============================================================================


class SafeOperationResponse(BaseModel):
    """Payload-free owner-visible operation projection."""

    operation_id: str
    operation_kind: str
    admission_class: str
    owner_scope: str
    chat_id: Optional[str]
    parent_operation_id: Optional[str]
    connection_generation: Optional[str]
    request_generation: Optional[str]
    state: str
    phase_code: Optional[str]
    terminal_code: Optional[str]
    safe_summary: Optional[str]
    retry_after_ms: Optional[int]
    state_revision: int
    accepted_at: str
    queue_deadline_at: Optional[str]
    started_at: Optional[str]
    terminal_at: Optional[str]
    updated_at: str
    purge_after: Optional[str]


class AcceptedOperationSubmissionResponse(BaseModel):
    accepted: bool
    operation: SafeOperationResponse


class RefusedOperationSubmissionResponse(BaseModel):
    accepted: bool
    code: str
    retryable: bool
    retry_after_ms: Optional[int]


OperationSubmissionResponse = Union[
    AcceptedOperationSubmissionResponse,
    RefusedOperationSubmissionResponse,
]


class RuntimeMetricResponse(BaseModel):
    """One payload-free, low-cardinality runtime metric sample."""

    name: str
    value: Union[int, float]
    labels: Dict[str, str]


class RuntimeMetricsResponse(BaseModel):
    metrics: List[RuntimeMetricResponse]


operation_router = APIRouter(prefix="/api", tags=["Operations"])


def _utc_json(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("operation timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _safe_operation_json(operation: SafeOperationProjection) -> dict[str, Any]:
    """Serialize exactly the reviewed public operation fields."""

    return {
        "operation_id": str(operation.operation_id),
        "operation_kind": operation.operation_kind,
        "admission_class": operation.admission_class.value,
        "owner_scope": operation.owner_scope.value,
        "chat_id": operation.chat_id,
        "parent_operation_id": (
            str(operation.parent_operation_id)
            if operation.parent_operation_id is not None
            else None
        ),
        "connection_generation": (
            str(operation.connection_generation)
            if operation.connection_generation is not None
            else None
        ),
        "request_generation": (
            str(operation.request_generation)
            if operation.request_generation is not None
            else None
        ),
        "state": operation.state.value,
        "phase_code": operation.phase_code,
        "terminal_code": operation.terminal_code,
        "safe_summary": operation.safe_summary,
        "retry_after_ms": operation.retry_after_ms,
        "state_revision": operation.state_revision,
        "accepted_at": _utc_json(operation.accepted_at),
        "queue_deadline_at": _utc_json(operation.queue_deadline_at),
        "started_at": _utc_json(operation.started_at),
        "terminal_at": _utc_json(operation.terminal_at),
        "updated_at": _utc_json(operation.updated_at),
        "purge_after": _utc_json(operation.purge_after),
    }


def _authenticated_owner_partitions(user_id: str) -> tuple[OperationOwner, ...]:
    """Partitions an authenticated user may reconcile through the REST API."""

    return tuple(
        OperationOwner(
            owner_scope=scope,
            owner_user_id=user_id,
            connection_scope_id=None,
        )
        for scope in (OwnerScope.USER, OwnerScope.SCHEDULE)
    )


def _parse_reconciliation_id(value: str, *, detail: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=detail) from exc


def _query_visible_operation(coordinator, user_id: str, operation_id: uuid.UUID):
    for owner in _authenticated_owner_partitions(user_id):
        try:
            return coordinator.query_operation(
                owner=owner,
                operation_id=operation_id,
            )
        except OperationNotFoundError:
            continue
    raise OperationNotFoundError("operation not found")


def _query_visible_submission(coordinator, user_id: str, submission_id: uuid.UUID):
    for owner in _authenticated_owner_partitions(user_id):
        try:
            return coordinator.reconcile_submission(
                owner=owner,
                submission_id=submission_id,
            )
        except OperationNotFoundError:
            continue
    raise OperationNotFoundError("operation submission not found")


_ADMISSION_OPERATION_KINDS = {
    AdmissionClass.GLOBAL: "global_capacity",
    AdmissionClass.INTERACTIVE: "connection_frame",
    AdmissionClass.BACKGROUND: "background_chat",
    AdmissionClass.SCHEDULED: "scheduled_occurrence",
    AdmissionClass.MAINTENANCE: "maintenance",
    AdmissionClass.SYSTEM: "system",
}


def _refresh_runtime_admission_metrics(orchestrator) -> None:
    """Refresh configured admission gauges without blocking the event loop."""

    coordinator = orchestrator.work_admission
    observability = orchestrator.runtime_observability
    for admission_class, operation_kind in _ADMISSION_OPERATION_KINDS.items():
        try:
            class_status = coordinator.inspect_admission_class(admission_class)
        except AdmissionConfigurationError:
            # Tests and intentionally reduced deployments may configure only
            # the classes they execute. An absent class is not reported as a
            # misleading zero-capacity class.
            continue
        observability.observe_admission(
            class_status,
            operation_kind=operation_kind,
        )


def _runtime_metric_snapshot(orchestrator) -> tuple[Any, ...]:
    """Merge the runtime and voice collectors into one de-duplicated export."""

    primary = orchestrator.runtime_observability
    voice_services = getattr(orchestrator, "voice_services", None)
    voice = getattr(voice_services, "observability", None)
    collectors = (primary,) if voice is None or voice is primary else (primary, voice)
    samples: dict[tuple[str, tuple[tuple[str, str], ...]], Any] = {}
    for collector in collectors:
        for sample in collector.snapshot():
            key = (sample.name, tuple(sorted(sample.labels.items())))
            samples.setdefault(key, sample)
    return tuple(samples[key] for key in sorted(samples))


@operation_router.get(
    "/operations/{operation_id}",
    response_model=SafeOperationResponse,
    summary="Reconcile an accepted operation",
    description=(
        "Returns the authenticated user's retained, payload-free user- or "
        "schedule-owned operation projection. Unknown, expired, connection-"
        "owned, and non-owner-visible identities share the same non-disclosing "
        "not-found response; UUID possession never grants access."
    ),
    responses={404: {"model": ErrorResponse}},
)
async def get_operation(
    request: Request,
    operation_id: str,
    response: Response,
    user_id: str = Depends(require_user_id),
):
    coordinator = _get_orchestrator(request).work_admission
    parsed = _parse_reconciliation_id(operation_id, detail="Operation not found")
    try:
        operation = await asyncio.to_thread(
            _query_visible_operation,
            coordinator,
            user_id,
            parsed,
        )
    except OperationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Operation not found") from exc
    response.headers["Cache-Control"] = "no-store"
    return _safe_operation_json(operation)


@operation_router.get(
    "/operation-submissions/{submission_id}",
    response_model=OperationSubmissionResponse,
    summary="Reconcile an operation submission",
    description=(
        "Returns the authenticated user's original retained user- or schedule-"
        "owned acceptance or safe admission refusal without exposing the "
        "submitted payload or digest. Unknown, expired, connection-owned, and "
        "non-owner-visible identities are not distinguished."
    ),
    responses={404: {"model": ErrorResponse}},
)
async def get_operation_submission(
    request: Request,
    submission_id: str,
    response: Response,
    user_id: str = Depends(require_user_id),
):
    coordinator = _get_orchestrator(request).work_admission
    parsed = _parse_reconciliation_id(
        submission_id,
        detail="Operation submission not found",
    )
    try:
        result = await asyncio.to_thread(
            _query_visible_submission,
            coordinator,
            user_id,
            parsed,
        )
    except OperationNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail="Operation submission not found",
        ) from exc

    response.headers["Cache-Control"] = "no-store"
    if result.accepted:
        return {
            "accepted": True,
            "operation": _safe_operation_json(result.operation),
        }
    return {
        "accepted": False,
        "code": result.code,
        "retryable": result.retryable,
        "retry_after_ms": result.retry_after_ms,
    }


@operation_router.get(
    "/runtime-reliability/metrics",
    response_model=RuntimeMetricsResponse,
    summary="Inspect runtime reliability metrics",
    description=(
        "Returns a verified administrator an authenticated, payload-free "
        "snapshot of effective admission gauges and bounded reliability "
        "counters, including the feature-080 background operation latency "
        "aggregates, with a no-store response. Deployment-wide diagnostics "
        "require the existing Keycloak admin role: an authenticated non-admin "
        "principal is denied with 403 before any collector or admission "
        "inspection occurs. Ordinary owner-scoped operation reconciliation "
        "remains available to non-admin users on its own endpoints."
    ),
    responses={
        403: {
            "description": (
                "The principal is authenticated but lacks the admin role; "
                "denied before any metrics or admission inspection."
            )
        },
    },
    dependencies=[Depends(verify_admin)],
)
async def get_runtime_reliability_metrics(
    request: Request,
    response: Response,
    _user_id: str = Depends(require_user_id),
):
    orchestrator = _get_orchestrator(request)
    await asyncio.to_thread(_refresh_runtime_admission_metrics, orchestrator)
    response.headers["Cache-Control"] = "no-store"
    return {
        "metrics": [
            {
                "name": sample.name,
                "value": sample.value,
                "labels": dict(sample.labels),
            }
            for sample in _runtime_metric_snapshot(orchestrator)
        ]
    }


# =============================================================================
# Chat Router
# =============================================================================

chat_router = APIRouter(prefix="/api/chats", tags=["Chat"])


@chat_router.get(
    "",
    response_model=ChatListResponse,
    summary="List recent chats",
    description="Returns a list of recent chat sessions for the authenticated user, ordered by most recent first.",
)
async def list_chats(
    request: Request,
    limit: int = 20,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    chats = await asyncio.to_thread(orch.history.get_recent_chats, limit=limit, user_id=user_id)
    return ChatListResponse(chats=[ChatSummary(**c) for c in chats])


@chat_router.post(
    "",
    response_model=ChatCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new chat",
    description=(
        "Creates a new empty chat session and returns its ID. "
        "Feature 013 / FR-006: pass `agent_id` in the body to bind the new "
        "chat to a specific agent so the UI can render the active-agent "
        "indicator. Omit to leave the chat unbound."
    ),
)
async def create_chat(
    request: Request,
    body: Optional[ChatCreateRequest] = None,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    agent_id = body.agent_id if body is not None else None
    chat_id = await asyncio.to_thread(orch.history.create_chat, user_id=user_id, agent_id=agent_id)
    return ChatCreateResponse(chat_id=chat_id, agent_id=agent_id)


@chat_router.get(
    "/{chat_id}",
    response_model=ChatDetailResponse,
    summary="Load a chat",
    description="Returns full chat details including all messages.",
    responses={404: {"model": ErrorResponse}},
)
async def get_chat(
    request: Request,
    chat_id: str,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    chat = await asyncio.to_thread(orch.history.get_chat, chat_id, user_id=user_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return ChatDetailResponse(chat=ChatDetail(**chat))


@chat_router.delete(
    "/{chat_id}",
    response_model=DeleteResponse,
    summary="Delete a chat",
    description="Deletes a chat session and all its messages.",
    responses={404: {"model": ErrorResponse}},
)
async def delete_chat(
    request: Request,
    chat_id: str,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    voice_mutation = await asyncio.to_thread(
        orch.history.delete_chat,
        chat_id,
        user_id=user_id,
    )
    voice_services = getattr(orch, "voice_services", None)
    if voice_services is not None:
        try:
            await voice_services.handle_chat_unavailable(voice_mutation)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The chat transaction already durably fenced the voice rows.
            # Short-lived grants and the lease sweep remain cleanup backstops.
            logger.warning(
                "voice_chat_media_cleanup_unavailable",
                exc_info=True,
            )
    # Feature 028 (spec edge case): another tab time-traveling through this
    # chat must have its historical view ended gracefully, not left staring
    # at a snapshot of a chat that no longer exists.
    for ws in list(getattr(orch, "ui_clients", []) or []):
        try:
            if (orch._get_user_id(ws) == user_id
                    and orch._ws_active_chat.get(id(ws)) == chat_id):
                orch._ws_active_chat.pop(id(ws), None)
                if orch._ws_timeline_mode.pop(id(ws), None):
                    await orch._safe_send(ws, json.dumps({
                        "type": "workspace_timeline_mode", "active": False,
                    }))
                await orch._safe_send(ws, json.dumps({
                    "type": "chat_deleted", "chat_id": chat_id,
                }))
        except Exception:
            logger.debug("delete_chat socket notification failed", exc_info=True)
    return DeleteResponse(message=f"Chat {chat_id} deleted")


@chat_router.get(
    "/{chat_id}/steps",
    summary="Load persistent step entries for a chat",
    description=(
        "Feature 014 — returns the chronological sequence of step entries "
        "(tool calls / agent hand-offs / orchestrator phases) recorded for "
        "this chat. Used by the frontend on initial chat load and on "
        "WebSocket reconnect to rehydrate the in-chat step trail. All "
        "fields are PHI-redacted on the way out (defense-in-depth)."
    ),
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
async def get_chat_steps(
    request: Request,
    chat_id: str,
    response: Response,
    user_id: str = Depends(require_user_id),
):
    """Return all chat_steps rows for ``chat_id``, redacted and ordered.

    Read-time healing: any row with ``status='in_progress'`` older than
    30 seconds for which no active task exists is reported as
    ``status='interrupted'`` (FR-021 reconnect path). The healing is not
    persisted on this read; an orphaned-row sweep happens elsewhere.
    """
    orch = _get_orchestrator(request)

    # Ownership + existence check (matches the get_chat pattern).
    chat = await asyncio.to_thread(orch.history.get_chat, chat_id, user_id=user_id)
    if not chat:
        # Try to differentiate "exists for another user" vs "does not exist".
        plane_runtime, plane_repositories = _plane_boundary(orch)

        def _exists_for_administration() -> bool:
            with plane_runtime.transaction() as transaction:
                return (
                    plane_repositories.history.conversations.get_for_administration(
                        transaction,
                        conversation_id=chat_id,
                    )
                    is not None
                )

        if await asyncio.to_thread(_exists_for_administration):
            raise HTTPException(status_code=403, detail="Chat not owned by user")
        raise HTTPException(status_code=404, detail="Chat not found")

    response.headers["Cache-Control"] = "no-store"

    try:
        from shared.phi_redactor import redact

        records = await asyncio.to_thread(
            orch.history.list_chat_steps,
            chat_id,
            user_id,
        )

        # Read-time healing: orphan in-progress rows older than 30 s when
        # there is no active task on this chat — only mutate the response,
        # not the DB.
        import time as _time
        now_ms = int(_time.time() * 1000)
        active = orch.task_manager.get_active_task(chat_id)

        steps = []
        for record in records:
            status_value = record.status.value
            if (
                status_value == "in_progress"
                and active is None
                and now_ms - record.started_at > 30_000
            ):
                status_value = "interrupted"
            # Defense-in-depth re-redaction on every field that could
            # ever contain PHI.
            args_text, _ = redact(record.args_truncated, kind="args")
            result_text, _ = redact(record.result_summary, kind="result")
            error_text, _ = redact(record.error_message, kind="error")
            steps.append({
                "id": record.step_id,
                "chat_id": record.conversation_id,
                "turn_message_id": record.turn_message_id,
                "kind": record.kind,
                "name": record.name,
                "status": status_value,
                "args_truncated": args_text,
                "args_was_truncated": record.args_was_truncated,
                "result_summary": result_text,
                "result_was_truncated": record.result_was_truncated,
                "error_message": error_text,
                "started_at": record.started_at,
                "ended_at": record.ended_at,
            })
        return {"chat_id": chat_id, "steps": steps}
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover — defensive
        logger.error("Failed to load chat steps: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to load steps")


@chat_router.post(
    "/{chat_id}/messages",
    response_model=ChatMessageResponse,
    summary="Send a chat message",
    description=(
        "Sends a message in the specified chat. This endpoint **acknowledges** the message immediately. "
        "The actual response (LLM tool routing, UI components) will stream back over the WebSocket connection. "
        "Connect to `ws://<host>:<port>/ws` and register to receive streaming results."
    ),
    responses={404: {"model": ErrorResponse}},
)
async def send_message(
    request: Request,
    chat_id: str,
    body: ChatMessageRequest,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)

    # Ensure chat exists
    if not await asyncio.to_thread(orch.history.get_chat, chat_id, user_id=user_id):
        await asyncio.to_thread(orch.history.create_chat, chat_id, user_id=user_id)

    # Try to dispatch the message for processing via the orchestrator.
    # If a WebSocket client is connected for this user, results stream to them.
    # If not, results are still saved to history.
    dispatched = False
    try:
        for ws in orch.ui_clients:
            if ws in orch.ui_sessions:
                ws_user_id = orch.ui_sessions[ws].get("sub", "legacy")
                if ws_user_id == user_id:
                    asyncio.create_task(
                        orch.handle_chat_message(ws, body.message, chat_id, body.display_message, user_id=user_id)
                    )
                    dispatched = True
                    break

        if not dispatched:
            asyncio.create_task(
                orch.handle_chat_message(None, body.message, chat_id, body.display_message, user_id=user_id)
            )
    except Exception as e:
        logger.warning(f"Could not dispatch chat message for async processing: {e}")

    return ChatMessageResponse(
        chat_id=chat_id,
        status="accepted",
        message="Message received. Results will stream via WebSocket." if dispatched
               else "Message received. No WebSocket client connected — results will be saved to history.",
    )


@chat_router.get(
    "/{chat_id}/usage",
    summary="Get LLM token usage for a chat",
    description="Returns accumulated LLM token usage (prompt, completion, total) for a conversation.",
)
async def get_chat_usage(
    request: Request,
    chat_id: str,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    usage = orch.token_usage.get(chat_id, {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    })
    return JSONResponse(content={"chat_id": chat_id, "usage": usage})


# =============================================================================
# Component Router
# =============================================================================

component_router = APIRouter(prefix="/api", tags=["Components"])


@component_router.get(
    "/chats/{chat_id}/components",
    response_model=ComponentListResponse,
    summary="Get saved components for a chat",
    description="Returns all saved UI components for the specified chat session.",
)
async def get_components(
    request: Request,
    chat_id: str,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    components = await asyncio.to_thread(
        orch.history.get_saved_components, chat_id, user_id=user_id
    )
    return ComponentListResponse(components=[SavedComponent(**c) for c in components])


@component_router.post(
    "/chats/{chat_id}/components",
    response_model=ComponentSaveResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Save a component",
    description="Save a UI component to the specified chat session.",
)
async def save_component(
    request: Request,
    chat_id: str,
    body: ComponentSaveRequest,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    # Feature 028 (D18/FR-026): explicit saves are a deprecated alias — route
    # dict payloads through the workspace so the row gets a stable identity
    # and every connected client sees the mutation (ui_upsert), mirroring the
    # WS save_component reconciliation.
    if not isinstance(body.component_data, dict):
        raise HTTPException(
            status_code=400,
            detail="component_data must be a component object",
        )

    async def _save():
        ops = await orch.workspace.aupsert(
            chat_id, user_id, [dict(body.component_data)]
        )
        if not ops:
            raise RuntimeError("component save produced no workspace mutation")
        await orch.workspace.asnapshot(chat_id, user_id, cause="save")
        return ops[0]["component_id"]

    semantic_id = await _run_atomic_canvas_mutation(
        orch,
        chat_id=chat_id,
        user_id=user_id,
        mutation=_save,
    )
    row = await orch.workspace.aget_by_component_id(
        chat_id, user_id, semantic_id
    )
    if row is None:
        raise HTTPException(status_code=500, detail="Saved component unavailable")
    component_id = row["id"]
    await orch.send_ui_upsert(
        None,
        chat_id,
        user_id,
        [{"op": "upsert", "component_id": semantic_id,
          "component": row["component_data"], "created": True}],
    )
    return ComponentSaveResponse(
        component=SavedComponent(
            id=component_id,
            chat_id=chat_id,
            component_data=row["component_data"],
            component_type=body.component_type,
            title=body.title or body.component_type.replace("_", " ").title(),
            created_at=int(time.time() * 1000),
        )
    )


@component_router.delete(
    "/components/{component_id}",
    response_model=DeleteResponse,
    summary="Delete a saved component",
    responses={404: {"model": ErrorResponse}},
)
async def delete_component(
    request: Request,
    component_id: str,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    # Feature 028 (D18/FR-026): resolve the row first so the workspace
    # identity can be removed on every client and the removal snapshotted +
    # audited — a REST delete must not mutate the workspace invisibly.
    row = await asyncio.to_thread(orch.history.get_component_by_id, component_id, user_id=user_id)
    ws_component_id = None
    chat_id_for_row = row.get("chat_id") if row else None
    if not row or not chat_id_for_row:
        raise HTTPException(status_code=404, detail="Component not found")
    async def _delete():
        identity = await _workspace_identity_for_saved_row(
            orch,
            chat_id=chat_id_for_row,
            user_id=user_id,
            row=row,
            supplied_id=component_id,
        )
        if not identity or not await orch.workspace.aremove(
            chat_id_for_row, user_id, identity
        ):
            raise HTTPException(status_code=404, detail="Component not found")
        await orch.workspace.asnapshot(
            chat_id_for_row, user_id, cause="remove"
        )
        return identity

    ws_component_id = await _run_atomic_canvas_mutation(
        orch,
        chat_id=chat_id_for_row,
        user_id=user_id,
        mutation=_delete,
    )
    await orch.send_ui_upsert(None, chat_id_for_row, user_id, [
        {"op": "remove", "component_id": ws_component_id}
    ])
    try:
        from audit.hooks import record_workspace_event
        asyncio.create_task(record_workspace_event(
            user_id=user_id, action="component_removed",
            chat_id=chat_id_for_row, component_id=ws_component_id,
        ))
    except Exception:
        logger.debug("workspace remove audit failed (REST)", exc_info=True)
    return DeleteResponse(message=f"Component {component_id} deleted")


@component_router.post(
    "/components/combine",
    response_model=ComponentCombineResponse,
    summary="Combine two components",
    description="Uses LLM to merge two saved components into a single cohesive component.",
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def combine_components(
    request: Request,
    body: ComponentCombineRequest,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    source = await asyncio.to_thread(orch.history.get_component_by_id, body.source_id, user_id=user_id)
    target = await asyncio.to_thread(orch.history.get_component_by_id, body.target_id, user_id=user_id)

    if not source or not target:
        raise HTTPException(status_code=404, detail="One or both components not found")

    chat_id = source["chat_id"]
    if target["chat_id"] != chat_id:
        raise HTTPException(status_code=400, detail="Components belong to different chats")

    async def _combine():
        fresh = []
        for original, supplied_id in (
            (source, body.source_id),
            (target, body.target_id),
        ):
            identity = await _workspace_identity_for_saved_row(
                orch,
                chat_id=chat_id,
                user_id=user_id,
                row=original,
                supplied_id=supplied_id,
            )
            current = (
                await orch.workspace.aget_by_component_id(
                    chat_id, user_id, identity
                )
                if identity
                else None
            )
            if current is None:
                raise HTTPException(status_code=404, detail="Component not found")
            fresh.append(current)
        result = await orch._combine_components_llm(fresh, mode="combine")
        if result.get("error"):
            raise HTTPException(status_code=400, detail=result["error"])
        return await _replace_workspace_components(
            orch,
            chat_id=chat_id,
            user_id=user_id,
            source_rows=fresh,
            replacements=result["components"],
            cause="combine",
        )

    removed_semantic, new_components, ops = (
        await _run_atomic_canvas_mutation(
            orch,
            chat_id=chat_id,
            user_id=user_id,
            mutation=_combine,
        )
    )
    new_components = await _refresh_saved_component_rows(
        orch, chat_id, new_components, ops, user_id
    )
    await orch.send_ui_upsert(
        None,
        chat_id,
        user_id,
        ([{"op": "remove", "component_id": cid} for cid in removed_semantic]
         + ops),
    )
    return ComponentCombineResponse(
        removed_ids=[body.source_id, body.target_id],
        new_components=[SavedComponent(**c) for c in new_components],
    )


@component_router.post(
    "/components/condense",
    response_model=ComponentCombineResponse,
    summary="Condense multiple components",
    description="Uses LLM to reduce multiple saved components into fewer cohesive components.",
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def condense_components(
    request: Request,
    body: ComponentCondenseRequest,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    component_pairs = []
    for cid in body.component_ids:
        comp = await asyncio.to_thread(orch.history.get_component_by_id, cid, user_id=user_id)
        if comp:
            component_pairs.append((cid, comp))

    if len(component_pairs) < 2:
        raise HTTPException(status_code=400, detail="Not enough valid components found (need at least 2)")

    components = [component for _cid, component in component_pairs]
    chat_id = components[0]["chat_id"]
    if any(component["chat_id"] != chat_id for component in components):
        raise HTTPException(status_code=400, detail="Components belong to different chats")

    async def _condense():
        fresh = []
        for supplied_id, original in component_pairs:
            identity = await _workspace_identity_for_saved_row(
                orch,
                chat_id=chat_id,
                user_id=user_id,
                row=original,
                supplied_id=supplied_id,
            )
            current = (
                await orch.workspace.aget_by_component_id(
                    chat_id, user_id, identity
                )
                if identity
                else None
            )
            if current is None:
                raise HTTPException(status_code=404, detail="Component not found")
            fresh.append(current)
        result = await orch._combine_components_llm(fresh, mode="condense")
        if result.get("error"):
            raise HTTPException(status_code=400, detail=result["error"])
        return await _replace_workspace_components(
            orch,
            chat_id=chat_id,
            user_id=user_id,
            source_rows=fresh,
            replacements=result["components"],
            cause="condense",
        )

    removed_semantic, new_components, ops = (
        await _run_atomic_canvas_mutation(
            orch,
            chat_id=chat_id,
            user_id=user_id,
            mutation=_condense,
        )
    )
    new_components = await _refresh_saved_component_rows(
        orch, chat_id, new_components, ops, user_id
    )
    await orch.send_ui_upsert(
        None,
        chat_id,
        user_id,
        ([{"op": "remove", "component_id": cid} for cid in removed_semantic]
         + ops),
    )
    return ComponentCombineResponse(
        removed_ids=body.component_ids,
        new_components=[SavedComponent(**c) for c in new_components],
    )


# =============================================================================
# Agent Router
# =============================================================================

agent_router = APIRouter(prefix="/api/agents", tags=["Agents"])


def _external_identity_metadata(card: Any, provider: str) -> dict[str, str] | None:
    metadata = getattr(card, "metadata", None) or {}
    declared = metadata.get("external_identity") if isinstance(metadata, dict) else None
    if not isinstance(declared, dict) or declared.get("provider") != provider:
        return None
    authorization_url = declared.get("authorization_url")
    if not isinstance(authorization_url, str):
        return None
    parsed = urlsplit(authorization_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return None
    return {
        "provider": provider,
        "authorization_url": authorization_url,
        "label": str(declared.get("label") or provider.upper()),
    }


@agent_router.get(
    "/{agent_id}/external-identities/{provider}/start",
    summary="Start a verified external-identity link",
)
async def start_external_identity_link(
    request: Request,
    agent_id: str,
    provider: str,
    user_id: str = Depends(require_user_id_or_web_session),
):
    """Bind a short-lived state token to this Astral browser session."""
    orch = _get_orchestrator(request)
    card = orch.agent_cards.get(agent_id)
    declaration = _external_identity_metadata(card, provider) if card else None
    from orchestrator.agent_identity import identity_projection_trusted
    from orchestrator.external_identity_links import create_link_state, link_secret_for

    secret = link_secret_for(agent_id)
    if declaration is None or secret is None or not identity_projection_trusted(card):
        raise HTTPException(status_code=404, detail="Identity linking is unavailable")
    state_token = create_link_state(
        agent_id=agent_id,
        provider=provider,
        user_id=user_id,
        secret=secret,
    )
    separator = "&" if "?" in declaration["authorization_url"] else "?"
    destination = (
        declaration["authorization_url"]
        + separator
        + urlencode({"state": state_token})
    )
    return RedirectResponse(destination, status_code=status.HTTP_302_FOUND)


@agent_router.get(
    "/{agent_id}/external-identities/{provider}/callback",
    summary="Complete a verified external-identity link",
)
async def complete_external_identity_link(
    request: Request,
    agent_id: str,
    provider: str,
    state: str,
    assertion: str,
    user_id: str = Depends(require_user_id_or_web_session),
):
    """Verify the PanAtlas handoff, persist it, and refresh live sessions."""
    orch = _get_orchestrator(request)
    card = orch.agent_cards.get(agent_id)
    declaration = _external_identity_metadata(card, provider) if card else None
    from orchestrator.agent_identity import identity_projection_trusted
    from orchestrator.external_identity_links import (
        IdentityAlreadyLinkedError,
        IdentityLinkError,
        claims_with_saved_identities,
        link_secret_for,
        store_verified_identity,
        verify_link_handoff,
    )

    secret = link_secret_for(agent_id)
    if declaration is None or secret is None or not identity_projection_trusted(card):
        raise HTTPException(status_code=404, detail="Identity linking is unavailable")
    try:
        plane_runtime, repositories = _plane_boundary(orch)
        verified = verify_link_handoff(
            agent_id=agent_id,
            provider=provider,
            user_id=user_id,
            state_token=state,
            assertion_token=assertion,
            secret=secret,
        )
        await asyncio.to_thread(
            store_verified_identity,
            None,
            user_id=user_id,
            agent_id=agent_id,
            provider=provider,
            subject=verified["subject"],
            issuer=verified["issuer"],
            state_nonce=verified["state_nonce"],
            plane_runtime=plane_runtime,
            plane_repositories=repositories,
        )
    except IdentityAlreadyLinkedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IdentityLinkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    for websocket, session in list(orch.ui_sessions.items()):
        if (session or {}).get("sub") == user_id:
            orch.ui_sessions[websocket] = await asyncio.to_thread(
                claims_with_saved_identities,
                None,
                user_id,
                session,
                plane_runtime=plane_runtime,
                plane_repositories=repositories,
            )
    return RedirectResponse(
        "/?external_identity=orcid-linked",
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@agent_router.get(
    "",
    response_model=AgentListResponse,
    summary="List connected agents",
    description="Returns all agents currently connected to the orchestrator, including their tools, capabilities, and ownership info.",
)
async def list_agents(
    request: Request,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    plane_runtime, repositories = _plane_boundary(orch)

    def _read_agent_index():
        with plane_runtime.transaction() as transaction:
            ownership = repositories.agents.list_ownership_for_administration(
                transaction,
                limit=5000,
            )
            disabled = repositories.tool_policy_state.list_disabled_agents(
                transaction,
                owner_id=user_id,
            )
        return {record.agent_id: record for record in ownership}, set(disabled)

    ownership_map, disabled_set = await asyncio.to_thread(_read_agent_index)
    agents = []
    for agent_id, card in orch.agent_cards.items():
        # Hide draft agents that aren't live yet
        if await asyncio.to_thread(orch._is_draft_agent, agent_id):
            continue
        ownership = ownership_map.get(agent_id)
        agents.append(AgentInfo(
            id=card.agent_id,
            name=card.name,
            description=card.description,
            tools=[
                AgentTool(name=s.id, description=s.description, input_schema=s.input_schema)
                for s in card.skills
            ],
            security_flags=orch.security_flags.get(agent_id, {}),
            status="connected",
            owner_email=None if ownership is None else ownership.owner_email,
            is_public=False if ownership is None else ownership.is_public,
            disabled=agent_id in disabled_set,
        ))
    return AgentListResponse(agents=agents)


@agent_router.get(
    "/{agent_id}/permissions",
    response_model=AgentPermissionsResponse,
    summary="Get agent scope permissions",
    description="Returns the current user's scope-based permissions for the specified agent.",
)
async def get_agent_permissions(
    request: Request,
    agent_id: str,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    card = orch.agent_cards.get(agent_id)
    if not card:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    # Feature 013 / FR-015: on first read after the migration ships, lazily
    # backfill per-tool rows from the legacy scope state so users don't
    # have to re-toggle previously consented permissions. Idempotent —
    # subsequent reads insert nothing.
    available_tools = [s.id for s in card.skills]
    tool_descriptions = {s.id: s.description for s in card.skills}

    def _read_permission_state():
        """Backfill + resolve all permission views off the event loop."""
        try:
            orch.tool_permissions.backfill_per_tool_rows(user_id, agent_id)
        except Exception as e:  # pragma: no cover — defensive logging only
            logger.warning(f"Per-tool backfill failed for user={user_id} agent={agent_id}: {e}")
        return (
            orch.tool_permissions.get_agent_scopes(user_id, agent_id),
            orch.tool_permissions.get_tool_scope_map(agent_id),
            orch.tool_permissions.get_effective_permissions(user_id, agent_id, available_tools),
            orch.tool_permissions.get_effective_tool_permissions(user_id, agent_id),
            orch.tool_permissions.get_tool_overrides(user_id, agent_id),
        )

    (scopes, tool_scope_map, permissions, per_tool_permissions,
     tool_overrides) = await asyncio.to_thread(_read_permission_state)
    return AgentPermissionsResponse(
        agent_id=agent_id,
        agent_name=card.name,
        scopes=scopes,
        tool_scope_map=tool_scope_map,
        permissions=permissions,
        per_tool_permissions=per_tool_permissions,
        tool_overrides=tool_overrides,
        tool_descriptions=tool_descriptions,
        security_flags=orch.security_flags.get(agent_id, {}),
    )


@agent_router.put(
    "/{agent_id}/permissions",
    response_model=AgentPermissionsResponse,
    summary="Update agent scope permissions",
    description="Update the current user's scope-based permissions for the specified agent. Scopes: tools:read, tools:write, tools:search, tools:system, tools:files, tools:execute.",
)
async def set_agent_permissions(
    request: Request,
    agent_id: str,
    body: AgentPermissionsRequest,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    card = orch.agent_cards.get(agent_id)
    if not card:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")

    # Feature 057 (finding: private-agent grant hole): a caller may manage
    # permissions only on an agent they are allowed to use. A user-created agent
    # is private to its owner — without this, another user could grant THEMSELVES
    # scopes on it and then invoke it, running the owner's device-hosted tool as
    # themselves (a cross-user break, SC-003). Non-user-agents (built-ins/public)
    # are unaffected: can_user_use_agent returns True for them.
    from orchestrator.user_agents import can_user_use_agent
    if not await asyncio.to_thread(
        can_user_use_agent,
        orch.user_agent_registry,
        user_id,
        agent_id,
    ):
        raise HTTPException(
            status_code=403,
            detail=f"You cannot manage permissions for agent '{agent_id}'.")

    tool_scope_map = orch.tool_permissions.get_tool_scope_map(agent_id)
    legacy_payload = body.per_tool_permissions is None and (
        body.scopes is not None or body.tool_overrides is not None
    )

    # Feature 013 / preferred shape: per-tool, per-kind toggles.
    if body.per_tool_permissions is not None:
        # Validate every (tool, kind) pair is applicable to that tool
        # (FR-014). Reject the whole payload on any mismatch so partial
        # writes never leave a half-applied state.
        for tool_name, kind_map in body.per_tool_permissions.items():
            required = tool_scope_map.get(tool_name)
            if required is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Tool '{tool_name}' is not registered for agent '{agent_id}'.",
                )
            for kind in kind_map.keys():
                if kind != required:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Permission kind '{kind}' does not apply to tool "
                            f"'{tool_name}' (required: '{required}')."
                        ),
                    )
        def _apply_per_tool():
            """Write per-tool rows and mirror them into agent_scopes off-loop."""
            for tool_name, kind_map in body.per_tool_permissions.items():
                for kind, enabled in kind_map.items():
                    orch.tool_permissions.set_tool_permission(
                        user_id, agent_id, tool_name, kind, bool(enabled)
                    )
            # Mirror up to the agent_scopes layer so the legacy filter path
            # remains coherent: a scope is enabled at the legacy layer iff at
            # least one tool of that kind is now enabled per-tool.
            scope_state = orch.tool_permissions.get_agent_scopes(user_id, agent_id)
            derived: Dict[str, bool] = {**scope_state}
            per_tool = orch.tool_permissions.get_effective_tool_permissions(user_id, agent_id)
            for tool_name, kind_map in per_tool.items():
                for kind, enabled in kind_map.items():
                    if enabled:
                        derived[kind] = True
            orch.tool_permissions.set_agent_scopes(user_id, agent_id, derived)

        await asyncio.to_thread(_apply_per_tool)

    # Legacy shape for transitional clients — write scopes, then reflect
    # the change into per-tool rows so the new model stays in sync.
    elif legacy_payload:
        def _apply_legacy():
            """Write legacy scope state and re-derive per-tool rows off-loop."""
            if body.scopes is not None:
                orch.tool_permissions.set_agent_scopes(user_id, agent_id, body.scopes)
            if body.tool_overrides is not None:
                orch.tool_permissions.set_tool_overrides(user_id, agent_id, body.tool_overrides)
            # Re-derive per-tool rows from the new scope+override state.
            for tool_name, required_scope in tool_scope_map.items():
                scope_enabled = orch.tool_permissions.is_scope_enabled(
                    user_id, agent_id, required_scope
                )
                override_disabled = (body.tool_overrides or {}).get(tool_name, True) is False
                orch.tool_permissions.set_tool_permission(
                    user_id, agent_id, tool_name, required_scope,
                    bool(scope_enabled and not override_disabled),
                )

        await asyncio.to_thread(_apply_legacy)
        logger.warning(
            "Legacy scope-shaped permissions update accepted for user=%s agent=%s "
            "(legacy_scope_update=true)",
            user_id,
            agent_id,
        )
    else:
        raise HTTPException(
            status_code=400,
            detail="Request body must include either 'per_tool_permissions' or 'scopes'.",
        )

    available_tools = [s.id for s in card.skills]
    tool_descriptions = {s.id: s.description for s in card.skills}

    def _read_back():
        """Re-read the resolved permission views off the event loop."""
        return (
            orch.tool_permissions.get_agent_scopes(user_id, agent_id),
            orch.tool_permissions.get_effective_permissions(user_id, agent_id, available_tools),
            orch.tool_permissions.get_effective_tool_permissions(user_id, agent_id),
            orch.tool_permissions.get_tool_overrides(user_id, agent_id),
        )

    (scopes, permissions, per_tool_permissions,
     tool_overrides) = await asyncio.to_thread(_read_back)
    logger.info(
        "Agent permissions updated: user=%s agent=%s shape=%s tools_changed=%d",
        user_id,
        agent_id,
        "per_tool" if body.per_tool_permissions is not None else "legacy_scope",
        len(body.per_tool_permissions or {}),
    )
    return AgentPermissionsResponse(
        agent_id=agent_id,
        agent_name=card.name,
        scopes=scopes,
        tool_scope_map=tool_scope_map,
        permissions=permissions,
        per_tool_permissions=per_tool_permissions,
        tool_overrides=tool_overrides,
        tool_descriptions=tool_descriptions,
        security_flags=orch.security_flags.get(agent_id, {}),
    )


# ── Feature 013: User Tool-Selection Preference ──────────────────────────
# Per-user, per-agent in-chat tool-picker selection. Persisted as a JSON
# value under user_preferences.tool_selection.<agent_id>. The orchestrator
# narrows the LLM's tool list to this subset on each chat dispatch.

user_router = APIRouter(prefix="/api/users/me", tags=["User"])


@user_router.get(
    "/tool-selection",
    response_model=ToolSelectionResponse,
    summary="Get the current user's saved tool selection for an agent",
    description=(
        "Feature 013 / FR-024: returns the in-chat tool-picker subset the "
        "user previously saved for the given agent. `selected_tools=null` "
        "means no narrowing — orchestrator falls back to the full "
        "permission-allowed set."
    ),
)
async def get_user_tool_selection(
    request: Request,
    agent_id: str,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    if agent_id not in orch.agent_cards:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    selected = await asyncio.to_thread(
        orch.tool_permissions.get_tool_selection, user_id, agent_id
    )
    return ToolSelectionResponse(agent_id=agent_id, selected_tools=selected)


@user_router.put(
    "/tool-selection",
    response_model=ToolSelectionResponse,
    summary="Save the current user's tool selection for an agent",
    description=(
        "Feature 013 / FR-024. Empty arrays are rejected (FR-021 — UI gate). "
        "The list MUST be a strict subset of the agent's permission-allowed "
        "tools; tools blocked by scope/per-tool permissions are rejected."
    ),
)
async def set_user_tool_selection(
    request: Request,
    body: ToolSelectionUpdate,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    card = orch.agent_cards.get(body.agent_id)
    if not card:
        raise HTTPException(status_code=404, detail=f"Agent '{body.agent_id}' not found")
    # FR-021 defensive check — UI blocks send when zero, but a stray
    # empty PUT still must be rejected.
    if not body.selected_tools:
        raise HTTPException(
            status_code=400, detail="empty_selection_not_allowed"
        )
    agent_tool_ids = {s.id for s in card.skills}
    invalid = [t for t in body.selected_tools if t not in agent_tool_ids]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Tools not part of agent '{body.agent_id}': {invalid}",
        )
    blocked = await asyncio.to_thread(
        lambda: [
            t for t in body.selected_tools
            if not orch.tool_permissions.is_tool_allowed(user_id, body.agent_id, t)
        ]
    )
    if blocked:
        raise HTTPException(
            status_code=400,
            detail=f"Tools blocked by scope/per-tool permissions: {blocked}",
        )
    await asyncio.to_thread(
        orch.tool_permissions.set_tool_selection,
        user_id,
        body.agent_id,
        body.selected_tools,
    )
    logger.info(
        "Tool selection updated: user=%s agent=%s tools=%d action=set",
        user_id,
        body.agent_id,
        len(body.selected_tools),
    )
    return ToolSelectionResponse(
        agent_id=body.agent_id,
        selected_tools=body.selected_tools,
    )


@user_router.delete(
    "/tool-selection",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Reset the current user's tool selection for an agent",
    description=(
        "Feature 013 / FR-025: clears the saved selection so subsequent "
        "queries fall back to the agent's full permission-allowed set."
    ),
)
async def clear_user_tool_selection(
    request: Request,
    agent_id: str,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    if agent_id not in orch.agent_cards:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    cleared = await asyncio.to_thread(
        orch.tool_permissions.clear_tool_selection, user_id, agent_id
    )
    logger.info(
        "Tool selection updated: user=%s agent=%s action=reset cleared=%s",
        user_id,
        agent_id,
        cleared,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@user_router.put(
    "/agent-enabled",
    response_model=AgentEnabledResponse,
    summary="Toggle the user's per-agent disabled state",
    description=(
        "Feature 013 follow-up: per-user, agent-wide on/off switch. "
        "Disabling an agent removes it from the orchestrator's chat "
        "dispatch for THIS user only — scopes/per-tool permissions "
        "are NOT modified, so re-enabling resumes the prior state. "
        "Other users are unaffected."
    ),
)
async def set_user_agent_enabled(
    request: Request,
    body: AgentEnabledUpdate,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    if body.agent_id not in orch.agent_cards:
        raise HTTPException(status_code=404, detail=f"Agent '{body.agent_id}' not found")
    await asyncio.to_thread(
        orch.tool_permissions.set_agent_disabled,
        user_id,
        body.agent_id,
        not body.enabled,
    )
    logger.info(
        "Agent enabled state updated: user=%s agent=%s enabled=%s",
        user_id,
        body.agent_id,
        body.enabled,
    )
    return AgentEnabledResponse(agent_id=body.agent_id, enabled=body.enabled)


# ── Agent Visibility ──────────────────────────────────────────────────


@agent_router.put(
    "/{agent_id}/visibility",
    summary="Toggle agent public/private visibility",
    description="Set whether an agent is publicly available or private. Only the agent owner can change visibility.",
)
async def set_agent_visibility(
    request: Request,
    agent_id: str,
    body: AgentVisibilityRequest,
    payload: dict = Depends(get_current_user_payload),
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    plane_runtime, repositories = _plane_boundary(orch)

    def _set_visibility():
        with plane_runtime.transaction() as transaction:
            ownership = repositories.agents.get_ownership(
                transaction,
                agent_id=agent_id,
            )
            if ownership is None:
                return "missing"
            if ownership.owner_email != payload.get("email", ""):
                return "forbidden"
            repositories.agents.set_visibility(
                transaction,
                agent_id=agent_id,
                owner_email=ownership.owner_email,
                is_public=body.is_public,
                updated_at=int(time.time() * 1000),
            )
            return "updated"

    result = await asyncio.to_thread(_set_visibility)
    if result == "missing":
        raise HTTPException(status_code=404, detail=f"No ownership record for agent '{agent_id}'")
    if result == "forbidden":
        raise HTTPException(status_code=403, detail="Only the agent owner can change visibility")
    return {"agent_id": agent_id, "is_public": body.is_public}


# ── Agent Credentials ──────────────────────────────────────────────────


@agent_router.get(
    "/{agent_id}/credentials",
    response_model=CredentialListResponse,
    summary="List stored credential keys for an agent",
    description="Returns the names of stored credentials (never the values) and the agent's declared credential requirements.",
)
async def get_agent_credentials(
    request: Request,
    agent_id: str,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    card = orch.agent_cards.get(agent_id)
    if not card:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    keys = await asyncio.to_thread(orch.credential_manager.list_credential_keys, user_id, agent_id)
    required = getattr(card, 'metadata', {}).get("required_credentials", []) if hasattr(card, 'metadata') else []
    return CredentialListResponse(
        agent_id=agent_id,
        agent_name=card.name,
        credential_keys=keys,
        required_credentials=required,
    )


@agent_router.put(
    "/{agent_id}/credentials",
    response_model=CredentialListResponse,
    summary="Set credentials for an agent",
    description="Store one or more encrypted credentials for the specified agent. Values are encrypted at rest.",
)
async def set_agent_credentials(
    request: Request,
    agent_id: str,
    body: CredentialSetRequest,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    card = orch.agent_cards.get(agent_id)
    if not card:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    await asyncio.to_thread(
        orch.credential_manager.set_bulk_credentials, user_id, agent_id, body.credentials
    )
    keys = await asyncio.to_thread(orch.credential_manager.list_credential_keys, user_id, agent_id)
    required = getattr(card, 'metadata', {}).get("required_credentials", []) if hasattr(card, 'metadata') else []
    response = CredentialListResponse(
        agent_id=agent_id,
        agent_name=card.name,
        credential_keys=keys,
        required_credentials=required,
    )

    # Save-time credential probe (FR-008): if the agent exposes a
    # `_credentials_check` tool, invoke it immediately so the user gets a
    # success/auth-failed/unreachable verdict back in the same response.
    skill_names = {getattr(s, "name", None) for s in getattr(card, "skills", [])}
    if "_credentials_check" in skill_names:
        try:
            claims, subject_token = _request_dispatch_identity(request, user_id)
            mcp_resp = await orch.execute_authorized_tool(
                claims=claims,
                user_id=user_id,
                agent_id=agent_id,
                tool_name="_credentials_check",
                arguments={},
                channel="rest",
                delegation_subject_token=subject_token,
                timeout=5.0,
            )
            verdict = "unreachable"
            detail = None
            if mcp_resp is None:
                verdict, detail = "unreachable", "no response from agent"
            elif mcp_resp.error:
                verdict, detail = "unreachable", mcp_resp.error.get("message")
            elif isinstance(mcp_resp.result, dict):
                verdict = mcp_resp.result.get("credential_test", "unexpected")
                detail = mcp_resp.result.get("detail")
            response.credential_test = verdict
            response.credential_test_detail = detail
        except Exception as e:
            # A failed probe must not block the credential save; surface it as unreachable.
            response.credential_test = "unreachable"
            response.credential_test_detail = f"Credential probe failed: {e}"

    return response


@agent_router.delete(
    "/{agent_id}/credentials/{credential_key}",
    response_model=CredentialDeleteResponse,
    summary="Delete a credential",
    description="Remove a single stored credential for the specified agent.",
)
async def delete_agent_credential(
    request: Request,
    agent_id: str,
    credential_key: str,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    card = orch.agent_cards.get(agent_id)
    if not card:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    await asyncio.to_thread(
        orch.credential_manager.delete_credential, user_id, agent_id, credential_key
    )
    return CredentialDeleteResponse(message=f"Credential '{credential_key}' deleted for agent '{agent_id}'")


# =============================================================================
# Draft Agent Router
# =============================================================================

draft_router = APIRouter(prefix="/api/agents/drafts", tags=["Draft Agents"])


def _get_lifecycle(request: Request):
    """Retrieve the AgentLifecycleManager from app state."""
    orch = _get_orchestrator(request)
    lifecycle = getattr(orch, 'lifecycle_manager', None)
    if lifecycle is None:
        raise HTTPException(status_code=503, detail="Agent lifecycle manager not initialized")
    return lifecycle


def _draft_store(orch):
    lifecycle = getattr(orch, "lifecycle_manager", None)
    store = vars(lifecycle).get("draft_store") if hasattr(lifecycle, "__dict__") else None
    if store is None:
        raise HTTPException(status_code=503, detail="Draft persistence not initialized")
    return store


def _find_user_websocket(orch, user_id: str):
    """Find the WebSocket connection for a given user_id (for progress updates)."""
    for ws, session in orch.ui_sessions.items():
        if session.get("user_id") == user_id:
            return ws
    return None


def _parse_json_field(value):
    """Parse a JSON string field, returning None if empty/null."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return __import__('json').loads(value)
        except (ValueError, TypeError):
            return value
    return value


def _backfill_validation_tools(validation_report: dict, slug: str, orch) -> dict:
    """Backfill 'tools' into a validation report from the orchestrator's agent cards."""
    if not validation_report or validation_report.get("tools"):
        return validation_report  # already has tools or no report
    agent_id = f"{slug.replace('_', '-')}-1"
    card = orch.agent_cards.get(agent_id) if orch else None
    if not card:
        return validation_report
    tools = []
    for skill in card.skills:
        schema = skill.input_schema or {}
        props = schema.get("properties", {})
        required = schema.get("required", []) if isinstance(schema.get("required"), list) else []
        params = []
        for pname, pinfo in props.items():
            if isinstance(pinfo, dict):
                params.append({
                    "name": pname,
                    "type": pinfo.get("type", "any"),
                    "description": pinfo.get("description", ""),
                    "required": pname in required,
                })
        tools.append({
            "name": skill.id,
            "description": skill.description or "",
            "scope": getattr(skill, "scope", "tools:read") or "tools:read",
            "parameters": params,
        })
    validation_report["tools"] = tools
    return validation_report


def _draft_to_response(draft: dict, orch=None) -> DraftAgentResponse:
    """Convert a raw draft dict to a DraftAgentResponse with parsed JSON fields."""
    validation_report = _parse_json_field(draft.get("validation_report"))
    if validation_report and orch:
        validation_report = _backfill_validation_tools(
            validation_report, draft["agent_slug"], orch
        )
    return DraftAgentResponse(
        id=draft["id"],
        user_id=draft["user_id"],
        agent_name=draft["agent_name"],
        agent_slug=draft["agent_slug"],
        description=draft["description"],
        tools_spec=_parse_json_field(draft.get("tools_spec")),
        skill_tags=_parse_json_field(draft.get("skill_tags")),
        packages=_parse_json_field(draft.get("packages")),
        status=draft["status"],
        generation_log=_parse_json_field(draft.get("generation_log")),
        security_report=_parse_json_field(draft.get("security_report")),
        validation_report=validation_report,
        error_message=draft.get("error_message"),
        port=draft.get("port"),
        review_notes=draft.get("review_notes"),
        reviewed_by=draft.get("reviewed_by"),
        refinement_history=_parse_json_field(draft.get("refinement_history")),
        required_credentials=_parse_json_field(draft.get("required_credentials")),
        created_at=draft.get("created_at"),
        updated_at=draft.get("updated_at"),
    )


@draft_router.get(
    "",
    response_model=DraftAgentListResponse,
    summary="List draft agents",
    description="Returns all draft agents belonging to the current user.",
)
async def list_drafts(
    request: Request,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    drafts = await asyncio.to_thread(
        _draft_store(orch).get_user_draft_agents,
        user_id,
    )
    return DraftAgentListResponse(
        drafts=[_draft_to_response(d, orch) for d in drafts]
    )


@draft_router.post(
    "",
    response_model=DraftAgentResponse,
    summary="Create a draft agent",
    description="Creates a new draft agent with the given specification. Does not generate code yet.",
)
async def create_draft(
    request: Request,
    body: DraftAgentCreateRequest,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    lifecycle = _get_lifecycle(request)
    try:
        draft = await lifecycle.create_draft(
            user_id=user_id,
            agent_name=body.agent_name,
            description=body.description,
            tools_spec=[t.model_dump() for t in body.tools] if body.tools else None,
            skill_tags=body.skill_tags,
            packages=body.packages,
        )
        return _draft_to_response(draft, orch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@draft_router.get(
    "/pending-review",
    response_model=DraftAgentListResponse,
    summary="List drafts pending admin review",
    description="Admin endpoint: returns all draft agents awaiting review.",
)
async def list_pending_review(
    request: Request,
    user_id: str = Depends(require_user_id),
    payload: dict = Depends(get_current_user_payload),
):
    roles = payload.get("roles", []) if payload else []
    if "admin" not in roles:
        raise HTTPException(status_code=403, detail="Admin role required")

    orch = _get_orchestrator(request)
    drafts = await asyncio.to_thread(_draft_store(orch).get_pending_review_drafts)
    return DraftAgentListResponse(
        drafts=[_draft_to_response(d, orch) for d in drafts]
    )


@draft_router.get(
    "/{draft_id}",
    response_model=DraftAgentResponse,
    summary="Get draft agent details",
)
async def get_draft(
    request: Request,
    draft_id: str,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    draft = await asyncio.to_thread(
        _draft_store(orch).get_owned_draft_agent,
        user_id,
        draft_id,
    )
    if not draft:
        raise HTTPException(status_code=404, detail="Draft agent not found")
    return _draft_to_response(draft, orch)


@draft_router.delete(
    "/{draft_id}",
    response_model=DeleteResponse,
    summary="Delete a draft agent",
    description="Stops the agent process, removes files, and deletes the record.",
)
async def delete_draft(
    request: Request,
    draft_id: str,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    draft = await asyncio.to_thread(
        _draft_store(orch).get_owned_draft_agent,
        user_id,
        draft_id,
    )
    if not draft:
        raise HTTPException(status_code=404, detail="Draft agent not found")

    lifecycle = _get_lifecycle(request)
    deleted = await lifecycle.delete_draft(draft_id)
    if not deleted:
        raise HTTPException(status_code=500, detail="Failed to delete draft agent")
    return DeleteResponse(message="Draft agent deleted successfully")


@draft_router.post(
    "/{draft_id}/generate",
    response_model=DraftAgentResponse,
    summary="Generate agent code",
    description="Triggers LLM code generation for the draft agent. Progress updates are sent via WebSocket.",
)
async def generate_draft(
    request: Request,
    draft_id: str,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    draft = await asyncio.to_thread(
        _draft_store(orch).get_owned_draft_agent,
        user_id,
        draft_id,
    )
    if not draft:
        raise HTTPException(status_code=404, detail="Draft agent not found")

    # 058 SC-002 — this endpoint generates for the SERVER-HOSTED (027) target,
    # which validates by executing the generated tools here. A BYO draft's code
    # is the user's and never runs on this host: it is generated + delivered
    # through the authoring flow (chrome_author_generate) only.
    from orchestrator.agent_lifecycle import BYO_ORIGIN
    if draft.get("origin") == BYO_ORIGIN:
        raise HTTPException(
            status_code=400,
            detail=("This is a user-hosted (BYO) agent. Generate it from the agent "
                    "authoring flow — it is delivered to your desktop client, not "
                    "run on the server."))

    lifecycle = _get_lifecycle(request)
    # Find user's WebSocket for progress updates
    ws = _find_user_websocket(orch, user_id)
    result = await lifecycle.generate_code(draft_id, websocket=ws)
    return _draft_to_response(result, orch)


@draft_router.post(
    "/{draft_id}/refine",
    response_model=DraftAgentResponse,
    summary="Refine agent via chat",
    description="Refines the agent's tool implementations based on a natural language message.",
)
async def refine_draft(
    request: Request,
    draft_id: str,
    body: DraftAgentRefineRequest,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    draft = await asyncio.to_thread(
        _draft_store(orch).get_owned_draft_agent,
        user_id,
        draft_id,
    )
    if not draft:
        raise HTTPException(status_code=404, detail="Draft agent not found")

    lifecycle = _get_lifecycle(request)
    ws = _find_user_websocket(orch, user_id)
    result = await lifecycle.refine_agent(draft_id, body.message, websocket=ws)
    return _draft_to_response(result, orch)


@draft_router.post(
    "/{draft_id}/test",
    response_model=DraftAgentResponse,
    summary="Start draft agent for testing",
    description="Launches the draft agent subprocess. The orchestrator will auto-discover it.",
)
async def test_draft(
    request: Request,
    draft_id: str,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    draft = await asyncio.to_thread(
        _draft_store(orch).get_owned_draft_agent,
        user_id,
        draft_id,
    )
    if not draft:
        raise HTTPException(status_code=404, detail="Draft agent not found")

    lifecycle = _get_lifecycle(request)
    ws = _find_user_websocket(orch, user_id)
    try:
        result = await lifecycle.start_draft_agent(draft_id, websocket=ws)
        return _draft_to_response(result, orch)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@draft_router.post(
    "/{draft_id}/stop",
    response_model=DraftAgentResponse,
    summary="Stop testing draft agent",
)
async def stop_draft(
    request: Request,
    draft_id: str,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    draft_store = _draft_store(orch)
    draft = await asyncio.to_thread(
        draft_store.get_owned_draft_agent,
        user_id,
        draft_id,
    )
    if not draft:
        raise HTTPException(status_code=404, detail="Draft agent not found")

    lifecycle = _get_lifecycle(request)
    await lifecycle.stop_draft_agent(draft_id)
    await asyncio.to_thread(
        draft_store.update_draft_agent,
        draft_id,
        status="generated",
    )
    updated = await asyncio.to_thread(
        draft_store.get_owned_draft_agent,
        user_id,
        draft_id,
    )
    return _draft_to_response(updated, orch)


@draft_router.post(
    "/{draft_id}/approve",
    response_model=DraftAgentResponse,
    summary="Approve draft agent",
    description="Runs comprehensive security analysis. Auto-approves if clean, sends to admin review if high-severity findings.",
)
async def approve_draft(
    request: Request,
    draft_id: str,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    draft = await asyncio.to_thread(
        _draft_store(orch).get_owned_draft_agent,
        user_id,
        draft_id,
    )
    if not draft:
        raise HTTPException(status_code=404, detail="Draft agent not found")

    lifecycle = _get_lifecycle(request)
    ws = _find_user_websocket(orch, user_id)
    result = await lifecycle.approve_agent(draft_id, websocket=ws)
    return _draft_to_response(result, orch)


@draft_router.post(
    "/{draft_id}/review",
    response_model=DraftAgentResponse,
    summary="Admin review: approve or reject",
    description="Admin endpoint to approve or reject a draft agent pending review.",
)
async def admin_review(
    request: Request,
    draft_id: str,
    body: AdminReviewRequest,
    user_id: str = Depends(require_user_id),
    payload: dict = Depends(get_current_user_payload),
):
    roles = payload.get("roles", []) if payload else []
    if "admin" not in roles:
        raise HTTPException(status_code=403, detail="Admin role required")

    lifecycle = _get_lifecycle(request)
    orch = _get_orchestrator(request)
    ws = None  # Could look up draft owner's WS for notification
    try:
        result = await lifecycle.admin_review(
            draft_id, body.decision, admin_user_id=user_id,
            notes=body.notes, websocket=ws
        )
        return _draft_to_response(result, orch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ─── Draft Agent Credentials ─────────────────────────────────────────────────

@draft_router.get(
    "/{draft_id}/credentials",
    summary="Get credential status for a draft agent",
    description="Returns required credentials and which ones the user has already stored.",
)
async def get_draft_credentials(
    request: Request,
    draft_id: str,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    draft = await asyncio.to_thread(
        _draft_store(orch).get_owned_draft_agent,
        user_id,
        draft_id,
    )
    if not draft:
        raise HTTPException(status_code=404, detail="Draft agent not found")

    agent_id = f"{draft['agent_slug'].replace('_', '-')}-1"
    stored_keys = await asyncio.to_thread(
        orch.credential_manager.list_credential_keys, user_id, agent_id
    )
    required = json.loads(draft.get("required_credentials") or "[]")

    return {
        "draft_id": draft_id,
        "agent_id": agent_id,
        "required_credentials": required,
        "stored_credential_keys": stored_keys,
    }


@draft_router.put(
    "/{draft_id}/credentials",
    summary="Set credentials for a draft agent",
    description="Store encrypted credentials for a draft agent before testing.",
)
async def set_draft_credentials(
    request: Request,
    draft_id: str,
    body: CredentialSetRequest,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    draft = await asyncio.to_thread(
        _draft_store(orch).get_owned_draft_agent,
        user_id,
        draft_id,
    )
    if not draft:
        raise HTTPException(status_code=404, detail="Draft agent not found")

    agent_id = f"{draft['agent_slug'].replace('_', '-')}-1"
    await asyncio.to_thread(
        orch.credential_manager.set_bulk_credentials, user_id, agent_id, body.credentials
    )
    stored_keys = await asyncio.to_thread(
        orch.credential_manager.list_credential_keys, user_id, agent_id
    )
    required = json.loads(draft.get("required_credentials") or "[]")

    return {
        "draft_id": draft_id,
        "agent_id": agent_id,
        "required_credentials": required,
        "stored_credential_keys": stored_keys,
    }


# =============================================================================
# Dashboard Router
# =============================================================================

dashboard_router = APIRouter(prefix="/api", tags=["System"])


@dashboard_router.get(
    "/dashboard",
    response_model=DashboardResponse,
    summary="Get system dashboard",
    description="Returns system configuration including connected agents and total tool count.",
)
async def get_dashboard(
    request: Request,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)

    def _build_dashboard():
        """Resolve per-agent effective permissions off the event loop."""
        agents = []
        total_tools = 0
        for agent_id, card in orch.agent_cards.items():
            available_tools = [s.id for s in card.skills]
            permissions = orch.tool_permissions.get_effective_permissions(
                user_id, agent_id, available_tools
            )
            total_tools += sum(1 for v in permissions.values() if v)
            agents.append(AgentInfo(
                id=card.agent_id,
                name=card.name,
                tools=[
                    AgentTool(name=s.id, description=s.description)
                    for s in card.skills
                ],
                status="connected",
            ))
        return agents, total_tools

    agents, total_tools = await asyncio.to_thread(_build_dashboard)
    return DashboardResponse(
        agents=agents,
        total_tools=total_tools,
        capabilities=orch.get_personal_agent_capabilities(),
    )


# =============================================================================
# Chrome Router  (Feature 042 — server-owned menu model, consumed by native clients)
# =============================================================================

chrome_router = APIRouter(prefix="/api/chrome", tags=["Chrome"])


def _roles_from_payload(payload: dict) -> list:
    """Realm + client roles from a validated JWT payload dict (mirrors
    web_auth._roles_from_token / chrome_events._roles, but on a payload)."""
    roles = list(((payload or {}).get("realm_access") or {}).get("roles") or [])
    for client in ((payload or {}).get("resource_access") or {}).values():
        roles.extend((client or {}).get("roles") or [])
    return roles


@chrome_router.get(
    "/menu",
    summary="Get the chrome menu model",
    description=(
        "Feature 042 — the single server-owned chrome model (top-bar controls + "
        "settings menu), role-filtered and feature-flag-resolved for the caller. "
        "Native clients (Windows/Android) render their chrome from this; it is the "
        "same model the web shell renders and the `chrome_menu` WS frame carries "
        "(Constitution XII: one definition, every client renders it). Admin items "
        "are omitted for non-admins; server-side authorization stays authoritative."
    ),
)
async def get_chrome_menu(payload: dict = Depends(get_current_user_payload)):
    from orchestrator.chrome_availability import projection_chrome_availability
    from webrender.chrome.menu_model import menu_model_dict
    # Native clients consume this — ADMIN TOOLS is web-only (include_admin=False)
    # and "Take the tour" is web-only (include_tour=False, feature 043).
    return menu_model_dict(
        _roles_from_payload(payload),
        include_admin=False,
        include_tour=False,
        **projection_chrome_availability(),
    )


@chrome_router.get(
    "/commands",
    summary="Get the caller's slash commands",
    description=(
        "Feature 077 — the curated slash commands plus the caller's own skill "
        "commands (Settings → My agents & skills), for typeahead discovery. "
        "Pure metadata: name + description; nothing here invokes anything."
    ),
)
async def get_chrome_commands(request: Request, payload: dict = Depends(get_current_user_payload)):
    from orchestrator import slash_commands
    items = [{"name": "/" + c["name"], "desc": c["description"], "mine": False}
             for c in slash_commands.command_list()]
    try:
        from orchestrator import user_skills
        store = user_skills.store_for(_get_orchestrator(request))
        user_id = str((payload or {}).get("sub") or "")
        if store is not None and user_id:
            for command, skill in sorted(store.command_map(user_id).items()):
                items.append({"name": "/" + command, "desc": skill.name, "mine": True})
    except Exception:
        logger.debug("user_skills: command listing skipped", exc_info=True)
    return {"commands": items}


# =============================================================================
# Task Router — Re-Act task state inspection
# =============================================================================

task_router = APIRouter(prefix="/api/tasks", tags=["Tasks"])


@task_router.get(
    "/{chat_id}",
    summary="Get active task state",
    description="Returns the authenticated user's Re-Act task state for a chat session.",
)
async def get_task_state(
    chat_id: str,
    request: Request,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    # 073: this route had no auth dependency and no ownership check, so
    # possession of a chat_id was enough to read another user's task state
    # including its step history. Task carries user_id, so filter on it rather
    # than trusting chat_id possession; a non-owner sees the same "none" a
    # stranger chat returns, which keeps the response non-disclosing.
    task = orch.task_manager.get_active_task(chat_id)
    if task and task.user_id == user_id:
        return task.to_dict()
    all_tasks = [
        t for t in orch.task_manager.get_chat_tasks(chat_id) if t.user_id == user_id
    ]
    if all_tasks:
        latest = max(all_tasks, key=lambda t: t.updated_at)
        return latest.to_dict()
    return {"state": "none", "chat_id": chat_id}


# =============================================================================
# 020-async-queries: Background task status endpoints
# =============================================================================

async_task_router = APIRouter(prefix="/api/async-tasks", tags=["AsyncTasks"])


@async_task_router.get(
    "/{task_id}",
    summary="Get background task status",
    description="Returns the status of an async background query task.",
)
async def get_async_task(
    task_id: str,
    request: Request,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    bg_task = await orch.async_task_manager.get(task_id)
    # 073: unknown and non-owned identities share one non-disclosing response,
    # so possession of a task_id never confirms that it exists.
    if bg_task is None or bg_task.user_id != user_id:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=404,
            content={"error": "Task not found", "task_id": task_id},
        )
    return {
        "task_id": bg_task.task_id,
        "chat_id": bg_task.chat_id,
        "status": bg_task.status.value,
        "created_at": bg_task.created_at.isoformat(),
        "completed_at": bg_task.completed_at.isoformat() if bg_task.completed_at else None,
        "output_count": len(bg_task.outputs),
        "errors": bg_task.errors,
    }


@async_task_router.get(
    "",
    summary="List background tasks",
    description="Returns a list of background tasks for the current user.",
)
async def list_async_tasks(
    request: Request,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    # 073: this previously called get_current_user_id(request) without await.
    # That helper is async AND a FastAPI dependency, so the call passed the
    # Request in as `payload` and bound a coroutine object to user_id. The
    # filter could therefore never match a real user, and the route had no auth
    # dependency at all. Resolving the identity through Depends fixes both.
    tasks = await orch.async_task_manager.list_for_user(user_id, limit=20)
    return {
        "tasks": [
            {
                "task_id": t.task_id,
                "chat_id": t.chat_id,
                "status": t.status.value,
                "created_at": t.created_at.isoformat(),
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                "output_count": len(t.outputs),
            }
            for t in tasks
        ],
    }


@async_task_router.post(
    "/{task_id}/cancel",
    summary="Cancel a background task",
    description="Requests cancellation of a running background task.",
)
async def cancel_async_task(
    task_id: str,
    request: Request,
    user_id: str = Depends(require_user_id),
):
    orch = _get_orchestrator(request)
    # 073: ownership is checked BEFORE cancelling, otherwise any caller holding
    # a task_id could cancel another user's running background work.
    bg_task = await orch.async_task_manager.get(task_id)
    if bg_task is None or bg_task.user_id != user_id:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=404,
            content={"error": "Task not found or already completed", "task_id": task_id},
        )
    cancelled = await orch.async_task_manager.cancel(task_id)
    if not cancelled:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=404,
            content={"error": "Task not found or already completed", "task_id": task_id},
        )
    return {"status": "cancelled", "task_id": task_id}


# =============================================================================
# 055-uniform-artifacts US5 (T043): Export Router — FF_ARTIFACT_EXPORT
# =============================================================================

export_router = APIRouter(prefix="/api/export", tags=["Export"])


class _ExportError(Exception):
    """CSV full-export refusal carrying the contract's `{error, detail?}` body."""

    def __init__(self, status_code: int, error: str, detail: Optional[str] = None):
        super().__init__(error)
        self.status_code = status_code
        self.error = error
        self.detail = detail

    def response(self) -> JSONResponse:
        body: Dict[str, Any] = {"error": self.error}
        if self.detail:
            body["detail"] = self.detail
        return JSONResponse(status_code=self.status_code, content=body)


def _flag_404(flag: str) -> None:
    """Contract (rest-endpoints.md): flag off ⇒ 404 as if the route were
    absent — the body matches FastAPI's unknown-path default, never a 500."""
    if not flags.is_enabled(flag):
        raise HTTPException(status_code=404, detail="Not Found")


def _export_filename(stem: str, ext: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(stem)).strip("._") or "export"
    return f"{safe}.{ext}"


def _csv_body(headers: List[Any], rows: List[Any]) -> str:
    def guard(cell: Any) -> str:
        # OWASP CSV-injection rule: neutralize leading formula triggers so a
        # spreadsheet opening the download treats the cell as text.
        s = "" if cell is None else str(cell)
        return "'" + s if s[:1] in ("=", "+", "-", "@") else s

    buf = io.StringIO()
    writer = csv.writer(buf)
    if headers:
        writer.writerow([guard(h) for h in headers])
    for r in rows:
        cells = r if isinstance(r, (list, tuple)) else [r]
        writer.writerow([guard(c) for c in cells])
    return buf.getvalue()


def _render_export_html(components: List[Dict[str, Any]], title: str) -> str:
    """Standalone export/share rendition (research D11): charts degrade down
    their table/text fallback ladder (a static file runs no Plotly), the
    workspace renders non-interactively (no buttons/chrome, provenance footer
    kept), and the whole page is the self-contained export document."""
    from rote.adapter import ComponentAdapter
    from rote.capabilities import DeviceProfile
    from webrender import (
        allowed_primitive_types,
        render_export_document,
        render_workspace,
    )

    profile = DeviceProfile.default()
    profile.supports_interactivity = False
    profile.supported_types = frozenset(
        t for t in allowed_primitive_types() if not t.endswith("_chart"))
    adapted = ComponentAdapter.adapt([dict(c) for c in components], profile)
    counts: Dict[str, int] = {}
    for c in components:
        mark = str(c.get("provenance") or "generated") if isinstance(c, dict) else "generated"
        counts[mark] = counts.get(mark, 0) + 1
    note = "Provenance: " + ", ".join(
        f"{counts[k]} {k}" for k in ("grounded", "estimated", "generated") if counts.get(k))
    return render_export_document(
        render_workspace(adapted, profile), title, note, date.today().isoformat())


async def _full_table_rows(orch, request: Request, user_id: str, chat_id: str,
                           cd: Dict[str, Any], total: int):
    """Re-invoke a paginated table's recorded source tool for the complete
    row set — the component_action gate sequence (retired/merged-agent
    handling, security flags + per-user permission, credential injection)
    without its canvas write-back: the export is serve-only."""
    from orchestrator.orchestrator import RETIRED_AGENT_IDS, remap_merged_source

    agent_id = cd.get("_source_agent") or ""
    tool_name = cd.get("_source_tool") or ""
    if not agent_id or not tool_name:
        raise _ExportError(503, "source_unavailable", "partial data available")
    if agent_id in RETIRED_AGENT_IDS:
        raise _ExportError(503, "source_retired", "partial data available")
    agent_id, tool_name = remap_merged_source(agent_id, tool_name)
    allowed, deny_reason = await asyncio.to_thread(
        orch._component_action_allowed, user_id, agent_id, tool_name)
    if not allowed:
        raise _ExportError(403, "forbidden", deny_reason)
    args = dict(cd.get("_source_params") or {})
    # Full-range paging under the same param names the pagination footer patches.
    args.update({"limit": int(total), "offset": 0})
    try:
        claims, subject_token = _request_dispatch_identity(request, user_id)
        result = await orch.execute_authorized_tool(
            claims=claims,
            user_id=user_id,
            agent_id=agent_id,
            tool_name=tool_name,
            arguments=args,
            channel="rest",
            chat_id=chat_id,
            delegation_subject_token=subject_token,
        )
    except Exception:
        logger.warning("csv export: source re-invoke failed", exc_info=True)
        result = None
    if result is not None and result.error:
        error_message = str(result.error.get("message", ""))
        if "restricted" in error_message.lower() or "permission" in error_message.lower():
            raise _ExportError(403, "forbidden", error_message)
    if result is not None and not result.error:
        for comp in result.ui_components or []:
            if isinstance(comp, dict) and str(comp.get("type") or "").strip().lower() == "table":
                return list(comp.get("headers") or []), list(comp.get("rows") or [])
    raise _ExportError(503, "source_unavailable", "partial data available")


@export_router.get(
    "/component/{component_id}.csv",
    summary="Export a table component as CSV",
    description=(
        "Downloads a table component's data as CSV. Paginated tables are "
        "re-invoked through the component_action pipeline for the complete "
        "row set; pass `stored_only=1` to skip the re-invoke and export the "
        "stored page (dead-source fallback)."
    ),
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
               422: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
async def export_component_csv(
    request: Request,
    component_id: str,
    chat_id: str,
    stored_only: int = 0,
    user_id: str = Depends(require_user_id_or_web_session),
):
    _flag_404("artifact_export")
    orch = _get_orchestrator(request)
    # The (chat_id, user_id)-scoped lookup IS the ownership check: foreign or
    # unknown components are indistinguishable (uniform 404).
    row = await orch.workspace.aget_by_component_id(chat_id, user_id, component_id)
    if row is None or not isinstance(row.get("component_data"), dict):
        raise HTTPException(status_code=404, detail="Component not found")
    cd = row["component_data"]
    if str(cd.get("type") or "").strip().lower() != "table":
        return JSONResponse(status_code=422, content={
            "error": "not_a_table",
            "detail": "Only table components can be exported as CSV",
        })
    headers = list(cd.get("headers") or [])
    rows = list(cd.get("rows") or [])
    try:
        total = int(cd.get("total_rows"))
    except (TypeError, ValueError):
        total = None
    full_export = False
    if total is not None and total > len(rows) and not stored_only:
        try:
            headers, rows = await _full_table_rows(
                orch, request, user_id, chat_id, cd, total
            )
            full_export = True
        except _ExportError as e:
            return e.response()
    body = await asyncio.to_thread(_csv_body, headers, rows)
    try:
        from audit.hooks import record_workspace_event
        await record_workspace_event(
            user_id=user_id, action="component_exported", chat_id=chat_id,
            component_id=component_id,
            detail={"format": "csv", "rows": len(rows), "full": full_export},
        )
    except Exception:
        logger.debug("export audit failed", exc_info=True)
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="{_export_filename(component_id, "csv")}"'},
    )


@export_router.get(
    "/canvas/{chat_id}.html",
    summary="Export the chat's workspace canvas as a standalone HTML document",
    responses={404: {"model": ErrorResponse}},
)
async def export_canvas_html(
    request: Request,
    chat_id: str,
    user_id: str = Depends(require_user_id_or_web_session),
):
    _flag_404("artifact_export")
    orch = _get_orchestrator(request)
    # Materialized designed layouts, (chat_id, user_id)-scoped — an unowned
    # chat and an empty canvas are indistinguishable (uniform 404).
    components = await asyncio.to_thread(orch._canvas_components, chat_id, user_id)
    if not components:
        raise HTTPException(status_code=404, detail="Nothing to export for this chat")
    html = await asyncio.to_thread(_render_export_html, components, "AstralDeep workspace")
    try:
        from audit.hooks import record_workspace_event
        await record_workspace_event(
            user_id=user_id, action="canvas_exported", chat_id=chat_id,
            detail={"format": "html", "components": len(components)},
        )
    except Exception:
        logger.debug("export audit failed", exc_info=True)
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="{_export_filename("canvas-" + chat_id, "html")}"'},
    )


# =============================================================================
# 055-uniform-artifacts US5 (T044): Share Router — FF_ARTIFACT_SHARING
# (DEFAULT OFF, fail-closed: every route 404s while the flag is off)
# =============================================================================

share_router = APIRouter(tags=["Share"])

_SHARE_PUBLIC_HEADERS = {
    "X-Robots-Tag": "noindex, nofollow",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy":
        "default-src 'none'; style-src 'unsafe-inline'; img-src data:",
}


class ShareCreateRequest(BaseModel):
    chat_id: str
    scope: str
    component_id: Optional[str] = None


@share_router.post(
    "/api/share",
    status_code=status.HTTP_201_CREATED,
    summary="Mint a revocable read-only share link",
    description=(
        "Snapshots the component or canvas rendition at mint time (a share "
        "never reads live workspace rows afterwards), runs the PHI gate "
        "fail-closed, and returns the share URL exactly once — the raw token "
        "is never stored."
    ),
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
               422: {"model": ErrorResponse}},
)
async def create_share(
    request: Request,
    body: ShareCreateRequest,
    user_id: str = Depends(require_user_id_or_web_session),
):
    _flag_404("artifact_sharing")
    orch = _get_orchestrator(request)
    scope = (body.scope or "").strip().lower()
    if scope not in ("component", "canvas"):
        return JSONResponse(status_code=422, content={
            "error": "invalid_scope", "detail": "scope must be 'component' or 'canvas'"})
    component_id = body.component_id if scope == "component" else None
    if scope == "component":
        if not component_id:
            return JSONResponse(status_code=422, content={
                "error": "invalid_request",
                "detail": "component-scoped share requires component_id"})
        row = await orch.workspace.aget_by_component_id(body.chat_id, user_id, component_id)
        if row is None or not isinstance(row.get("component_data"), dict):
            raise HTTPException(status_code=404, detail="Component not found")
        snapshot = [row["component_data"]]
        title = str(row["component_data"].get("title") or "Shared component")
    else:
        snapshot = await asyncio.to_thread(orch._canvas_components, body.chat_id, user_id)
        if not snapshot:
            raise HTTPException(status_code=404, detail="Nothing to share for this chat")
        title = "AstralDeep workspace"
    snapshot_html = await asyncio.to_thread(_render_export_html, snapshot, title)

    from orchestrator.artifact_share import (
        SharePHIRefusedError,
        SharingDisabledError,
        get_share_store,
    )
    try:
        minted = await get_share_store().mint(
            user_id=user_id, chat_id=body.chat_id, scope=scope,
            snapshot_html=snapshot_html, snapshot_json=snapshot,
            component_id=component_id,
        )
    except SharePHIRefusedError:
        return JSONResponse(status_code=403, content={"error": "phi_blocked"})
    except SharingDisabledError:
        raise HTTPException(status_code=404, detail="Not Found")
    except ValueError as e:
        return JSONResponse(status_code=422, content={
            "error": "invalid_request", "detail": str(e)})
    # The raw token appears exactly once — inside share_url; it is never
    # recoverable from storage or the owner listing.
    return {"id": minted["id"], "share_url": minted["share_url"],
            "created_at": minted["created_at"], "expires_at": minted["expires_at"]}


@share_router.get(
    "/api/share",
    summary="List my share grants",
    description="Owner's grants, newest first — metadata only, never token material.",
)
async def list_shares(user_id: str = Depends(require_user_id_or_web_session)):
    _flag_404("artifact_sharing")
    from orchestrator.artifact_share import get_share_store
    grants = await get_share_store().list_grants(user_id)
    return {"shares": grants}


@share_router.delete(
    "/api/share/{share_id}",
    summary="Revoke a share grant",
    description="Owner-scoped, idempotent, immediate — subsequent public opens refuse.",
    responses={404: {"model": ErrorResponse}},
)
async def revoke_share(share_id: int, user_id: str = Depends(require_user_id_or_web_session)):
    _flag_404("artifact_sharing")
    from orchestrator.artifact_share import get_share_store
    found = await get_share_store().revoke(user_id, share_id)
    if not found:
        raise HTTPException(status_code=404, detail="Share not found")
    return {"message": f"Share {share_id} revoked"}


@share_router.get("/share/{token}", include_in_schema=False)
async def serve_share(token: str):
    """PUBLIC (unauthenticated) snapshot serve. Uniform 404 for unknown,
    revoked, expired, and flag-off — indistinguishable from an absent route."""
    _flag_404("artifact_sharing")
    from orchestrator.artifact_share import get_share_store
    store = get_share_store()
    grant = await store.resolve(token)
    if grant is None:
        raise HTTPException(status_code=404, detail="Not Found")
    if not await store.record_open(grant):
        # The digest was revoked or expired after resolution but before the
        # open-count fence. Never serve through that revocation race.
        raise HTTPException(status_code=404, detail="Not Found")
    return HTMLResponse(content=grant["snapshot_html"], headers=_SHARE_PUBLIC_HEADERS)
