"""Observes an owner's original session for a paused resume or event wake without
granting route, mutation, or dispatch authority. Used by work_control_authority.py,
work_resume.py, and work_wake.py before they commit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Literal

from astralplane.repositories.assignment_models import AssignmentRecord
from astralplane.repositories.history import SessionCredentialFence, SessionExecutionObservation

from orchestrator import session_authority
from orchestrator.session_store import WebSessionStore
from orchestrator.work_submit_authority import AuthenticatedWorkRequest


@dataclass(frozen=True, slots=True)
class OperationControlAuthority:
    record: AssignmentRecord = field(repr=False)
    command: Literal["resume", "wake"]
    observation: SessionExecutionObservation = field(repr=False)
    _claims_json: str = field(repr=False)
    plane_runtime: object = field(repr=False)
    request_context: AuthenticatedWorkRequest | None = field(default=None, repr=False)
    request_credential: SessionCredentialFence | None = field(default=None, repr=False)

    @property
    def claims(self) -> dict:
        return json.loads(self._claims_json)


async def refresh_operation_control_authority(
    *, context: AuthenticatedWorkRequest, original: AssignmentRecord,
    command: Literal["resume", "wake"], sessions: WebSessionStore,
    expected_request_credential: SessionCredentialFence | None = None,
) -> OperationControlAuthority:
    try:
        if (type(context) is not AuthenticatedWorkRequest
                or type(original) is not AssignmentRecord
                or type(command) is not str or command not in {"resume", "wake"}
                or original.owner_id != context.owner_id
                or not isinstance(sessions, WebSessionStore)
                or sessions._sessions.plane_runtime is not context.plane_runtime):
            session_authority._unavailable()
        runtime = context.plane_runtime
        context.assert_current(runtime)
        if expected_request_credential is not None:
            credential = expected_request_credential
            if (type(credential) is not SessionCredentialFence
                    or credential.owner_id != context.owner_id
                    or credential.session_id != context.session_id
                    or credential.incarnation_id != original.operation["authority"]["reference_id"]
                    or (context.cookie_session is not None and context.cookie_session != (
                        credential.session_id, credential.incarnation_id))):
                session_authority._unavailable()

        def selected(value, owner_id, assignment_id):
            record, incarnation, expiry, deadline = session_authority._operation_reference(
                value, owner_id, assignment_id)
            if command == "resume":
                if record.lifecycle != "paused" or record.phase == "waiting_authorization":
                    session_authority._unavailable()
            elif (record.lifecycle != "active" or record.phase != "awaiting_event"
                    or record.operation.get("control", {}).get("wait") is None):
                session_authority._unavailable()
            return record, incarnation, expiry, deadline

        result = await session_authority._refresh_operation_session(
            owner_id=original.owner_id, assignment_id=original.assignment_id,
            sessions=sessions, plane_runtime=runtime, operation_context=selected,
            expected_record=original, request_expires_at=context.principal_expires_at,
            request_check=lambda now: context.assert_current(runtime, now=now),
            expected_session_credential=expected_request_credential)
        context.assert_current(runtime)
        return OperationControlAuthority(result.record, command, result.observation,
            result.claims_json, runtime, context, expected_request_credential)
    except Exception:
        raise session_authority.SessionAuthorityUnavailable("session_authority_unavailable") from None
