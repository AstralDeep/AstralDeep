"""Bind owner stop epochs to the application Plane facade and atomic audit records.
Current human or framework authority is checked in the same transaction as every
transition; emergency_stop_binding mounts this store into the coordinator.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from audit.hooks import actor_principal_from_claims
from audit.repository import AuditRepository
from audit.schemas import AuditEventCreate, AuditEventDTO
from orchestrator.emergency_stop import EmergencyStopRefused, current_effect_epoch
from orchestrator.framework_credentials import FrameworkCaller
from orchestrator.human_request_authority import CurrentHumanCaller
from orchestrator.plane_repository_context import plane_source_from_orchestrator
from persistent_agents.models import AssignmentError


class EmergencyStopStore:
    def __init__(self, orch):
        self.orch = orch
        source = plane_source_from_orchestrator(orch)
        self.runtime = source.plane_runtime
        self.repositories = source.plane_repositories
        self.repository = getattr(self.repositories, "stop_epochs", None)
        self.audit = getattr(orch, "audit_repo", None)

    def _current(self):
        source = plane_source_from_orchestrator(self.orch)
        if self.audit is None:
            self.audit = getattr(self.orch, "audit_repo", None)
        if (source.plane_runtime is not self.runtime or source.plane_repositories is not self.repositories
                or self.runtime.repositories is not self.repositories or self.repository is None
                or getattr(self.repositories, "stop_epochs", None) is not self.repository
                or type(self.audit) is not AuditRepository or getattr(self.orch, "audit_repo", None) is not self.audit
                or self.audit._audit.plane_runtime is not self.runtime
                or self.audit._audit.repository is not self.repositories.audit):
            raise EmergencyStopRefused("emergency_stop_unavailable", 503)

    @staticmethod
    def _record(record, owner_id):
        from astralplane.repositories.stop_epochs import OwnerStopRecord

        if type(record) is not OwnerStopRecord or record.owner_id != owner_id:
            raise EmergencyStopRefused("emergency_stop_unavailable", 503)
        return {"engaged": record.engaged, "epoch": record.epoch, "revision": record.revision,
                "engaged_at": record.engaged_at.timestamp() if record.engaged_at else None,
                "engaged_by": record.engaged_by, "reason": record.reason or ""}

    def load(self, owner_id):
        self._current()
        with self.runtime.transaction() as tx:
            record = self.repository.get(tx, owner_id=owner_id)
            self._current()
            return None if record is None else self._record(record, owner_id)

    def assert_running(self, tx, owner, operation_id=None, *, bind=False):
        from astralplane.repositories.stop_epochs import OwnerStoppedError, StopEpochConflictError
        from orchestrator.work_admission import OwnerScope

        self._current()
        if owner.owner_scope not in {OwnerScope.USER, OwnerScope.SCHEDULE}:
            return
        try:
            self.repository.assert_running(tx, owner_id=owner.owner_user_id,
                expected_epoch=current_effect_epoch(owner.owner_user_id))
            if operation_id is not None:
                method = self.repository.bind_operation if bind else self.repository.assert_operation
                method(tx, owner_id=owner.owner_user_id, operation_id=operation_id)
        except OwnerStoppedError:
            raise EmergencyStopRefused("emergency_stop_active", 423) from None
        except StopEpochConflictError:
            raise EmergencyStopRefused("emergency_stop_stale_epoch", 423) from None

    def _append(self, tx, *, owner_id, principal, action, outcome, detail):
        self._current()
        event = AuditEventCreate(event_id=str(uuid4()), actor_user_id=owner_id, auth_principal=principal,
            event_class="emergency_stop", action_type=action, description="Owner emergency stop control",
            inputs_meta=detail, correlation_id=str(uuid4()), started_at=datetime.now(UTC), outcome=outcome,
            outcome_detail=detail.get("denial") if outcome == "failure" else None)
        receipt = self.audit.insert_in_transaction(event, transaction=tx, plane_runtime=self.runtime)
        if (type(receipt) is not AuditEventDTO or receipt.event_id != event.event_id
                or receipt.event_class != event.event_class or receipt.action_type != event.action_type
                or receipt.outcome != event.outcome or receipt.correlation_id != event.correlation_id
                or receipt.inputs_meta != event.inputs_meta):
            raise EmergencyStopRefused("emergency_stop_unavailable", 503)

    async def transition(self, action, outcome, *, owner_id, claims=None, detail=None, caller=None):
        self._current()
        if action not in {"emergency_stop.engage", "emergency_stop.resume"} or outcome not in {"success", "failure"}:
            raise EmergencyStopRefused("emergency_stop_invalid", 422)
        detail = dict(detail or {})
        human = type(caller) is CurrentHumanCaller
        framework = type(caller) is FrameworkCaller
        if (not human and not framework) or caller.owner_id != owner_id:
            raise EmergencyStopRefused("emergency_stop_owner_authentication_required", 403)
        if human:
            caller.require_write()
            user, principal = actor_principal_from_claims(caller.claims)
            if user != owner_id:
                raise EmergencyStopRefused("emergency_stop_resume_denied", 403)
        else:
            if action != "emergency_stop.engage" or not caller.has_scope("operations.control"):
                raise EmergencyStopRefused("emergency_stop_owner_authentication_required", 403)
            principal = f"framework:{caller.credential_id}"

        def apply(tx, _repositories):
            self._current()
            if framework:
                credentials = getattr(getattr(self.orch, "framework_work_operations", None), "credentials", None)
                if credentials is None or credentials.plane_runtime is not self.runtime:
                    raise EmergencyStopRefused("emergency_stop_unavailable", 503)
                observation = credentials.fresh_observation(caller)
                if observation is None:
                    raise EmergencyStopRefused("emergency_stop_owner_authentication_required", 403)
                credential = credentials.assert_execution(tx, observation)
                if "operations.control" not in credential.credential.scopes:
                    raise EmergencyStopRefused("emergency_stop_owner_authentication_required", 403)
            current = self.repository.get(tx, owner_id=owner_id, for_update=True)
            revision = 0 if current is None else current.revision
            record = current
            if outcome == "success":
                if action == "emergency_stop.engage":
                    if current is None or not current.engaged:
                        record = self.repository.engage(tx, owner_id=owner_id, expected_revision=revision,
                            actor_id=owner_id, reason=detail.get("reason", ""), at=datetime.now(UTC))
                elif action == "emergency_stop.resume":
                    if current is None or not current.engaged:
                        raise EmergencyStopRefused("emergency_stop_not_engaged", 409)
                    if detail.get("expected_revision") != current.revision:
                        raise EmergencyStopRefused("emergency_stop_stale_revision", 409)
                    record = self.repository.resume(tx, owner_id=owner_id,
                        expected_revision=current.revision, expected_epoch=current.epoch, at=datetime.now(UTC))
            if record is not None:
                detail["epoch"] = record.epoch
                detail["revision"] = record.revision
            self._append(tx, owner_id=owner_id, principal=principal, action=action, outcome=outcome, detail=detail)
            self._current()
            return None if record is None else self._record(record, owner_id)

        def commit(tx, repositories):
            try:
                return apply(tx, repositories)
            except EmergencyStopRefused as exc:
                raise AssignmentError(exc.code, exc.status_code) from None
            except AssignmentError:
                raise
            except Exception:
                raise AssignmentError("emergency_stop_unavailable", 503) from None

        if human:
            try:
                return await caller.transaction(commit, expected_orchestrator=self.orch)
            except AssignmentError as exc:
                raise EmergencyStopRefused(exc.code, exc.status_code) from None

        def run():
            with self.runtime.transaction() as tx:
                return commit(tx, self.repositories)
        try:
            return await asyncio.to_thread(run)
        except AssignmentError as exc:
            raise EmergencyStopRefused(exc.code, exc.status_code) from None
