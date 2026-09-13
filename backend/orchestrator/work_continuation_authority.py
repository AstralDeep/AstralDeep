"""Private owner control observations; no route, mutation, or dispatch authority."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Literal

from astralplane.repositories.assignment_models import AssignmentRecord
from astralplane.repositories.history import SessionExecutionObservation

from orchestrator import session_authority
from orchestrator.session_store import WebSessionStore
from orchestrator.work_submit_authority import AuthenticatedWorkRequest


@dataclass(frozen=True, slots=True)
class OperationControlAuthority:
    """Ephemeral original command selection; committing callers recheck it.

    No subject token or execution-authority subtype is exposed. A resumed row
    must independently qualify through the ordinary execution resolver later.
    """

    record: AssignmentRecord = field(repr=False)
    command: Literal["resume", "wake"]
    observation: SessionExecutionObservation = field(repr=False)
    _claims_json: str = field(repr=False)
    plane_runtime: object = field(repr=False)

    @property
    def claims(self) -> dict:
        """Return a detached copy of the freshly verified original-owner claims."""
        return json.loads(self._claims_json)


async def refresh_operation_control_authority(
    *, context: AuthenticatedWorkRequest, original: AssignmentRecord,
    command: Literal["resume", "wake"], sessions: WebSessionStore,
) -> OperationControlAuthority:
    """Observe a paused resume or active event wake using its original session.

    Authenticate and resolve any accepted receipt before calling. The immutable
    record comes from that owner-scoped receipt miss, never request JSON. Remote
    refresh and JWT verification happen without SQL locks. The same original
    record/session is checked after awaits, and validity cannot exceed the
    requesting principal, original work, or refreshed session/JWT lifetime.

    This helper cannot resume or wake anything. The service must enforce command
    semantics, current capability, atomic audit, and repeat these observations
    around its mutation. Cancellation propagates; errors are data-free refusals.
    """
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
            request_check=lambda now: context.assert_current(runtime, now=now))
        context.assert_current(runtime)
        return OperationControlAuthority(result.record, command, result.observation,
            result.claims_json, runtime)
    except Exception:
        raise session_authority.SessionAuthorityUnavailable("session_authority_unavailable") from None
