"""Detached, immutable record types for persistent-assignment state, controls, actions,
and reconciliation outcomes; carry no authority themselves. Used throughout
repositories/assignments.py and AstralDeep's orchestrator/persistent_agents code.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import datetime
from enum import StrEnum
from typing import Any

from astralplane.repositories import _freeze
from astralplane.repositories.harness_capture import HarnessCapture


@dataclass(frozen=True, slots=True)
class _Record:
    def __post_init__(self) -> None:
        for entry in fields(self):
            value = getattr(self, entry.name)
            if isinstance(value, (Mapping, list, tuple)):
                object.__setattr__(self, entry.name, _freeze(value))


class AssignmentControl(StrEnum):
    REVISE = "revise"
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    REVOKE = "revoke"


@dataclass(frozen=True, slots=True)
class AssignmentDefinition(_Record):
    name: str
    instructions: str = field(repr=False)
    source: Mapping[str, Any] = field(repr=False)
    allowed_tools: tuple[str, ...]
    consented_scopes: tuple[str, ...]
    offline_grant_id: str | None = field(repr=False)
    limits: Mapping[str, Any]
    completion_condition: str | None = None
    conversation_id: str | None = None
    cost_quote_coverage: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class AssignmentOperationAuthority(_Record):
    owner_id: str = field(repr=False)
    origin: str
    reference_kind: str
    reference_id: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AssignmentOperationSpec(_Record):
    kind: str
    authority: AssignmentOperationAuthority = field(repr=False)
    deadline_at: datetime
    source_retention: str
    version: int = 2


@dataclass(frozen=True, slots=True)
class AssignmentRecord(_Record):
    assignment_id: str
    owner_id: str = field(repr=False)
    definition: AssignmentDefinition
    instruction_revision: int
    control_epoch: int
    state_version: int
    lifecycle: str
    phase: str
    next_wake_at: datetime | None
    wake_reason: str
    wake_generation: int
    checkpoint: Mapping[str, Any] = field(repr=False)
    tasks: tuple[Mapping[str, Any], ...] = field(repr=False)
    usage: Mapping[str, Any]
    created_at: datetime
    updated_at: datetime
    safe_error_code: str | None = None
    last_completed_generation: int = 0
    execution_profile: str = "persistent"
    operation: Mapping[str, Any] | None = field(default=None, repr=False)
    harness_capture: HarnessCapture | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class AssignmentFence(_Record):
    assignment_id: str
    owner_id: str = field(repr=False)
    instruction_revision: int
    control_epoch: int
    claim_generation: int
    claim_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class AssignmentClaim(_Record):
    assignment: AssignmentRecord
    fence: AssignmentFence = field(repr=False)
    lease_expires_at: datetime
    previous_operation_binding: Mapping[str, Any] | None = field(default=None, repr=False)
    approved_action_id: str | None = None


@dataclass(frozen=True, slots=True)
class AssignmentOperationBinding(_Record):
    operation_id: str
    execution_generation: int
    execution_lease_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class AssignmentControlResult(_Record):
    assignment: AssignmentRecord
    applied: bool
    invalidated_action_ids: tuple[str, ...] = ()
    begun_action_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AssignmentOperationRead(_Record):
    assignment: AssignmentRecord = field(repr=False)
    disposition: str
    continuation_supported: bool
    result_reference: str | None = field(default=None, repr=False)
    terminal_outcome: str | None = None


@dataclass(frozen=True, slots=True)
class AssignmentSourceEvent(_Record):
    event_id: str
    source_key: str
    item_key: str
    source_revision: str
    identity_digest: str
    context_digest: str
    context: Mapping[str, Any] = field(repr=False)
    disposition: str = "pending"
    result_digest: str | None = None


@dataclass(frozen=True, slots=True)
class AssignmentSourceBatch(_Record):
    batch_key: str
    batch_digest: str
    source_key: str
    configuration_digest: str
    expected_cursor_digest: str
    next_cursor: Any = field(repr=False)
    events: tuple[AssignmentSourceEvent, ...]


@dataclass(frozen=True, slots=True)
class AssignmentTask(_Record):
    task_id: str
    plan_key: str
    instruction_revision: int
    title: str
    instruction: str = field(repr=False)
    allowed_tools: tuple[str, ...]
    event_id: str | None = None
    parent_task_id: str | None = None
    depends_on: tuple[str, ...] = ()
    depth: int = 0
    state: str = "pending"
    attempt_count: int = 0
    task_generation: int = 0
    claim_generation: int = 0
    operation_id: str | None = None
    result_digest: str | None = None
    bounded_result: str | None = field(default=None, repr=False)
    provenance: Mapping[str, Any] = field(default_factory=dict, repr=False)
    incorporated_by: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AssignmentTaskClaim(_Record):
    fence: AssignmentFence = field(repr=False)
    task_id: str
    task_generation: int
    attempt_index: int
    task: AssignmentTask


@dataclass(frozen=True, slots=True)
class AssignmentTaskResult(_Record):
    state: str
    result_digest: str
    bounded_result: str = field(repr=False)
    provenance: Mapping[str, Any] = field(default_factory=dict, repr=False)
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class AssignmentResourceAmount(_Record):
    model_calls: int = 0
    tool_calls: int = 0
    tokens: int = 0
    elapsed_ms: int = 0
    spend_micro_units: int | None = None
    currency: str | None = None
    basis: Mapping[str, str] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class AssignmentInputReference(_Record):
    kind: str
    resource_id: str = field(repr=False)
    revision: int


@dataclass(frozen=True, slots=True)
class AssignmentTransientInput(_Record):
    binding_key_id: str
    payload_binding: str = field(repr=False)
    source_retention: str
    references: tuple[AssignmentInputReference, ...] = field(default=(), repr=False)
    reconstruction_kind: str = "model_messages"
    version: int = 1


@dataclass(frozen=True, slots=True)
class AssignmentResultDisposition(_Record):
    available: bool
    reason: str | None = None
    references: tuple[AssignmentInputReference, ...] = field(default=(), repr=False)
    binding_key_id: str | None = None
    version: int = 1


@dataclass(frozen=True, slots=True)
class AssignmentActionIntent(_Record):
    action_key: str
    request: Mapping[str, Any] = field(repr=False)
    request_digest: str
    maximum: AssignmentResourceAmount
    permission_digest: str
    precondition_digest: str
    task_id: str | None = None
    event_id: str | None = None
    sensitivity: str = "ordinary"
    interactive_only: bool = False
    boundary: str = "unreplayable"
    downstream_key: str | None = field(default=None, repr=False)
    quote_digest: str | None = None
    quote_expires_at: datetime | None = None
    approval_expires_at: datetime | None = None
    transient_input: AssignmentTransientInput | Mapping[str, Any] | None = field(
        default=None, repr=False
    )


@dataclass(frozen=True, slots=True)
class AssignmentActionRecord(_Record):
    action_id: str
    assignment_id: str
    owner_id: str = field(repr=False)
    intent: AssignmentActionIntent
    instruction_revision: int
    control_epoch: int
    state: str
    result: Mapping[str, Any] | None = field(default=None, repr=False)
    attempts: tuple[Mapping[str, Any], ...] = field(default=(), repr=False)
    interactive_proposal_id: str | None = None
    ever_started: bool = False


@dataclass(frozen=True, slots=True)
class AssignmentActionReservation(_Record):
    action: AssignmentActionRecord
    attempt_id: str
    maximum: AssignmentResourceAmount
    created: bool


@dataclass(frozen=True, slots=True)
class AssignmentDispatchPermit(_Record):
    action_id: str
    attempt_id: str
    dispatch_token: str = field(repr=False)
    request_digest: str
    binding: AssignmentOperationBinding = field(repr=False)


@dataclass(frozen=True, slots=True)
class AssignmentActionOutcome(_Record):
    outcome: str
    result_digest: str
    result: Mapping[str, Any] = field(default_factory=dict, repr=False)
    evidence_reference: str | None = None
    actual: AssignmentResourceAmount | None = None
    result_disposition: AssignmentResultDisposition | Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class AssignmentActionDecision(_Record):
    proposal_digest: str
    decision: str
    submission_id: str
    submission_digest: str
    permission_digest: str
    precondition_digest: str


@dataclass(frozen=True, slots=True)
class AssignmentActionReconciliation(_Record):
    prior_result_digest: str
    decision: str
    evidence_reference: str
    submission_id: str
    submission_digest: str


@dataclass(frozen=True, slots=True)
class AssignmentActionReconciliationPreparation(_Record):
    assignment: AssignmentRecord = field(repr=False)
    action: AssignmentActionRecord = field(repr=False)
    replayed: bool


@dataclass(frozen=True, slots=True)
class AssignmentOwnerWaitPreparation(_Record):
    assignment: AssignmentRecord = field(repr=False)
    replayed: bool
    invalidated_action_ids: tuple[str, ...] = ()
    begun_action_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AssignmentWakePreparation(_Record):
    assignment: AssignmentRecord = field(repr=False)
    replayed: bool


@dataclass(frozen=True, slots=True)
class AssignmentActivityRecord(_Record):
    activity_key: str
    activity_type: str
    title: str
    summary: str = field(repr=False)
    references: Mapping[str, Any] = field(default_factory=dict)
    activity_id: str | None = None
    sequence: int = 0
    created_at: datetime | None = None
    notification_state: str = "none"


@dataclass(frozen=True, slots=True)
class AssignmentEpisodeCompletion(_Record):
    expected_state_version: int
    checkpoint: Mapping[str, Any] = field(repr=False)
    completion_digest: str
    phase: str = "waiting"
    wake_reason: str = "cadence"
    next_wake_at: datetime | None = None
    incorporations: tuple[Mapping[str, str], ...] = ()
    event_receipts: tuple[Mapping[str, str], ...] = ()
    activity: AssignmentActivityRecord | None = None
    safe_error_code: str | None = None
    completed: bool = False
    terminal_outcome: str | None = None
    result_reference: str | None = field(default=None, repr=False)
    event_wait: Mapping[str, Any] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class AssignmentRecoveryResult(_Record):
    reclaimed_assignment_ids: tuple[str, ...] = ()
    operation_bindings: tuple[Mapping[str, Any], ...] = field(default=(), repr=False)
    uncertain_action_ids: tuple[str, ...] = ()
    terminal_assignments: tuple[AssignmentRecord, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class AssignmentRetentionResult(_Record):
    payload_redactions: int = 0
    activity_removals: int = 0
    capacity_holds: int = 0


@dataclass(frozen=True, slots=True)
class AssignmentOwnerRetirementResult(_Record):
    stopped_assignment_ids: tuple[str, ...] = ()
    deleted_assignment_ids: tuple[str, ...] = ()
    unresolved_action_ids: tuple[str, ...] = ()
    retained_assignment_ids: tuple[str, ...] = ()
