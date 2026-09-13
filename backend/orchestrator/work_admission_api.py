"""Closed fixed-research HTTP submission; exported without global registration.

The application supplies every capability. Request bytes carry intent only;
receipt replay remains in WorkSubmitService and never implies runner resumption.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import wraps
from uuid import UUID

from audit.repository import AuditRepository
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from llm_config.user_store import UserLLMConfigStore
from orchestrator.session_store import WebSessionStore
from orchestrator.work_submit import FixedResearchPreflight, WorkSubmitService
from orchestrator.work_submit_authority import authenticate_work_submission_request
from persistent_agents.models import AssignmentError
from persistent_agents.runner import AssignmentRunner
from persistent_agents.service import AssignmentService
from orchestrator.work_write_boundary import (
    BODY_SECONDS as _BODY_SECONDS,
    read_work_body,
    work_content_length as _content_length,
)

_ERRORS = {
    "work_submit_invalid",
    "work_submit_unavailable",
    "work_body_too_large",
    "work_body_invalid",
    "work_body_timeout",
    "work_disconnected",
    "work_authentication_required",
    "work_authority_unavailable",
    "work_origin_refused",
    "work_query_token_refused",
    "work_query_invalid",
    "work_json_required",
    "work_research_profile_unavailable",
    "work_research_budget_insufficient",
    "work_selection_not_found",
    "work_selection_changed",
    "work_selection_unavailable",
    "work_selected_agent_unavailable",
    "work_selected_agent_budget_refused",
    "work_repository_unavailable",
    "assignment_feature_disabled",
    "assignment_human_required",
    "assignment_owner_required",
    "assignment_owner_retired",
    "assignment_idempotency_conflict",
    "assignment_scope_revoked",
    "assignment_guidance_changed",
    "assignment_source_invalid",
    "assignment_phi_refused",
    "assignment_tool_unavailable",
    "assignment_scope_unavailable",
    "assignment_source_not_read_only",
    "assignment_authority_unavailable",
    "assignment_tool_bound_unavailable",
    "assignment_source_unavailable",
    "assignment_sensitive_content_refused",
    "assignment_phi_gate_unavailable",
    "assignment_destination_unavailable",
    "assignment_destination_not_found",
}


def _json(value, status=200):
    return JSONResponse(
        value, status_code=status, headers={"Cache-Control": "no-store"}
    )


def _unavailable():
    raise AssignmentError("work_submit_unavailable", 503)


def _orchestrator(app):
    orch = getattr(app.state, "orchestrator", None)
    if orch is None:
        root = getattr(app, "_root_app", None) or app
        orch = getattr(root.state, "orchestrator", None)
    return orch


@dataclass(frozen=True, slots=True, repr=False)
class _Composition:
    """Exact server objects, rechecked inside acceptance without external I/O."""

    app: object = field(repr=False)
    orch: object = field(repr=False)
    assignments: object = field(repr=False)
    sessions: object = field(repr=False)
    runtime: object = field(repr=False)
    audit: object = field(repr=False)
    config: object = field(repr=False)
    runner: object = field(repr=False)

    @classmethod
    def capture(cls, app):
        orch = _orchestrator(app)
        assignments = getattr(orch, "persistent_assignments", None)
        sessions = getattr(orch, "web_sessions", None)
        runtime = getattr(
            getattr(getattr(orch, "runtime_composition", None), "plane", None),
            "runtime",
            None,
        )
        value = cls(
            app,
            orch,
            assignments,
            sessions,
            runtime,
            getattr(orch, "audit_repo", None),
            getattr(orch, "_llm_store", None),
            getattr(orch, "persistent_assignment_runner", None),
        )
        value.assert_current()
        return value

    def assert_current(self):
        """Refuse replaced or cross-runtime application bindings, even on replay."""
        if (
            type(self.assignments) is not AssignmentService
            or type(self.sessions) is not WebSessionStore
            or type(self.audit) is not AuditRepository
            or type(self.config) is not UserLLMConfigStore
            or self.runtime is None
            or _orchestrator(self.app) is not self.orch
        ):
            _unavailable()
        plane = getattr(getattr(self.orch, "runtime_composition", None), "plane", None)
        if (
            getattr(plane, "runtime", None) is not self.runtime
            or getattr(plane, "repositories", None) is not self.runtime.repositories
            or self.assignments.orch is not self.orch
            or self.orch.persistent_assignments is not self.assignments
            or self.orch.web_sessions is not self.sessions
            or self.orch.audit_repo is not self.audit
            or self.orch._llm_store is not self.config
            or self.assignments.store.plane_runtime is not self.runtime
            or self.assignments.store.repository
            is not self.runtime.repositories.assignments
            or self.assignments.store.async_runtime.repositories
            is not self.runtime.repositories
            or self.sessions._sessions.plane_runtime is not self.runtime
            or self.sessions._sessions.repository
            is not self.runtime.repositories.history.sessions
            or self.audit._audit.plane_runtime is not self.runtime
            or self.audit._audit.repository is not self.runtime.repositories.audit
            or self.config._repository.plane_runtime is not self.runtime
            or self.config._repository.repository
            is not self.runtime.repositories.encrypted_llm_config
        ):
            _unavailable()

    def new_admission(self):
        """Check only new acceptance; stopped/absent runners cannot consume work."""
        self.assert_current()
        if (
            type(self.runner) is not AssignmentRunner
            or self.orch.persistent_assignment_runner is not self.runner
            or not self.runner.fixed_research_ready(
                service=self.assignments, sessions=self.sessions
            )
        ):
            _unavailable()
        loop = self.runner._loop
        if not isinstance(loop, asyncio.Task) or loop.done() or loop.cancelling():
            _unavailable()


async def _body(request, expected):
    """Retain the admission-specific time bound over shared raw framing."""
    return await read_work_body(request, expected, seconds=_BODY_SECONDS)


class WorkAdmissionRoute(APIRoute):
    """Never send exception details, parsed input or private authority to HTTP."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        @wraps(handler)
        async def safe(request):
            request.scope.setdefault("state", {}).pop("audit_claims", None)
            try:
                return await handler(request)
            except AssignmentError as error:
                known = (
                    type(error.code) is str
                    and error.code in _ERRORS
                    and type(error.status_code) is int
                    and error.status_code
                    in {
                        400,
                        401,
                        403,
                        404,
                        408,
                        409,
                        413,
                        415,
                        422,
                        429,
                        503,
                    }
                )
                return _json(
                    {"error": error.code if known else "work_submit_unavailable"},
                    error.status_code if known else 503,
                )
            except Exception:
                return _json({"error": "work_submit_unavailable"}, 503)

        return safe


# The containing operation router supplies the single /api prefix.
work_admission_router = APIRouter(
    prefix="/work/v1/operations", tags=["Work"], route_class=WorkAdmissionRoute
)


@work_admission_router.post("")
async def submit_work(request: Request):
    """Accept one fixed research intent or return its original authenticated receipt."""
    expected = _content_length(request)
    if "token" in request.query_params:
        raise AssignmentError("work_query_token_refused", 403)
    if request.query_params:
        raise AssignmentError("work_query_invalid", 422)
    composition = _Composition.capture(request.app)
    context = await authenticate_work_submission_request(
        request, sessions=composition.sessions, plane_runtime=composition.runtime
    )
    # Normal IAM verified these claims on its private snapshot. Only this
    # detached attribution reaches the existing outer HTTP audit middleware.
    request.state.audit_claims = context.claims
    raw = await _body(request, expected)
    composition.assert_current()
    service = WorkSubmitService(
        composition.assignments,
        composition.audit,
        composition.sessions,
        research_preflight=FixedResearchPreflight(composition.config),
        new_admission_check=composition.new_admission,
    )
    result = await service.submit(context, raw)
    composition.assert_current()
    record = result.record
    identity = UUID(record.assignment_id)
    if (
        identity.version != 4
        or str(identity) != record.assignment_id
        or record.owner_id != context.owner_id
        or type(record.state_version) is not int
        or not 1 <= record.state_version <= 2**63 - 1
        or type(result.created) is not bool
    ):
        _unavailable()
    return _json(
        {
            "id": record.assignment_id,
            "revision": record.state_version,
            "created": result.created,
        },
        201 if result.created else 200,
    )
