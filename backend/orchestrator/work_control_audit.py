"""Required same-transaction audit for owner Work commands.

The caller owns authentication and the command's repository locks. This adapter
cannot open a transaction or grant continuation; failures roll back the caller's
mutation and its receipt. Metadata contains identifiers and revisions only.
"""

from datetime import UTC, datetime

from astralplane.repositories.assignment_models import AssignmentRecord

from audit.repository import AuditRepository
from audit.schemas import AuditEventCreate, AuditEventDTO
from orchestrator.work_submit import _sync
from persistent_agents.models import AssignmentError


class WorkControlAudit:
    """Bind audit to the same application and Plane instance as the command."""

    def __init__(self, assignments):
        self.assignments = assignments
        self.store = assignments.store
        self.runtime = self.store.plane_runtime
        self.repository = self.store.repository
        self.async_runtime = self.store.async_runtime
        self.audit = getattr(assignments.orch, "audit_repo", None)

    def assert_store_current(self):
        """Bind the adapter that actually opens SQL, independently of audit."""
        if (self.assignments.store is not self.store
                or self.store.plane_runtime is not self.runtime
                or self.store.repository is not self.repository
                or self.repository is not self.runtime.repositories.assignments
                or self.store.async_runtime is not self.async_runtime
                or self.async_runtime.repositories is not self.runtime.repositories):
            raise AssignmentError("work_control_unavailable", 503)

    def assert_current(self):
        """Refuse a missing or replaced audit binding before committing work."""
        self.assert_store_current()
        if (type(self.audit) is not AuditRepository
                or getattr(self.assignments.orch, "audit_repo", None) is not self.audit
                or self.audit._audit.plane_runtime is not self.runtime
                or self.audit._audit.repository is not self.runtime.repositories.audit):
            raise AssignmentError("work_control_unavailable", 503)

    def append(self, transaction, *, owner_id, command, record):
        """Append a provisional audit record after mutation, before commit."""
        self.assert_current()
        if (command not in {"pause", "stop", "delete", "resume", "wake"}
                or type(record) is not AssignmentRecord or record.owner_id != owner_id):
            raise AssignmentError("work_control_unavailable", 503)
        now = datetime.now(UTC)
        event = AuditEventCreate(
            actor_user_id=owner_id, auth_principal=owner_id,
            event_class="settings", action_type="assignment_" + command,
            description="Work owner command", correlation_id=record.assignment_id,
            outcome="success", outputs_meta={
                "assignment_id": record.assignment_id,
                "instruction_revision": record.instruction_revision,
                "control_epoch": record.control_epoch,
            }, started_at=now, completed_at=now,
        )
        result = _sync(self.audit.insert_in_transaction(
            event, transaction=transaction, plane_runtime=self.runtime))
        if type(result) is not AuditEventDTO:
            raise AssignmentError("work_control_unavailable", 503)
        self.assert_current()
