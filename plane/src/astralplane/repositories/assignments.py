"""Owner-isolated persistence for assignment/operation lifecycle, controls, actions, and
event waits, fenced by caller-owned transactions and revision CAS. Backs AstralDeep's
orchestrator work_* and persistent_agents modules.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import asdict, fields, is_dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.assignment_models import (  # noqa: F401
    AssignmentActionDecision,
    AssignmentActionIntent,
    AssignmentActionOutcome,
    AssignmentActionReconciliation,
    AssignmentActionReconciliationPreparation,
    AssignmentActionRecord,
    AssignmentActionReservation,
    AssignmentActivityRecord,
    AssignmentClaim,
    AssignmentControl,
    AssignmentControlResult,
    AssignmentDefinition,
    AssignmentDispatchPermit,
    AssignmentEpisodeCompletion,
    AssignmentFence,
    AssignmentInputReference,
    AssignmentOperationAuthority,
    AssignmentOperationBinding,
    AssignmentOperationRead,
    AssignmentOperationSpec,
    AssignmentOwnerRetirementResult,
    AssignmentOwnerWaitPreparation,
    AssignmentRecord,
    AssignmentRecoveryResult,
    AssignmentResourceAmount,
    AssignmentResultDisposition,
    AssignmentRetentionResult,
    AssignmentSourceBatch,
    AssignmentSourceEvent,
    AssignmentTask,
    AssignmentTaskClaim,
    AssignmentTaskResult,
    AssignmentTransientInput,
    AssignmentWakePreparation,
)
from astralplane.repositories.framework_credentials import FrameworkCredentialRepository
from astralplane.repositories.harness_capture import harness_capture_from_value
from astralplane.repositories.harness_preferences import assert_harness_admission
from astralplane.repositories.history import (
    FrameworkCredentialObservation,
    SessionExecutionObservation,
    SessionRepository,
)
from astralplane.repositories.result_publication_models import (
    ResultPublicationContent,
    ResultPublicationPreparation,
    ResultPublicationProposal,
    ResultPublicationReceipt,
)
from astralplane.repositories.selected_input_models import (  # noqa: F401
    AssignmentSelectedInput,
    SelectedAgentReference,
    SelectedInputEnvelope,
    copy_envelope,
    decode_envelope,
)
from astralplane.repositories.work_admission import ExecutionFence, WorkAdmissionRepository

_DIMENSIONS = ("model_calls", "tool_calls", "tokens", "elapsed_ms")
_USAGE_BASIS_VALUES = frozenset({"observed", "estimated", "uncertain", "none"})


def _validate_usage_basis(basis):
    if not isinstance(basis, Mapping):
        raise RepositoryValidationError("usage basis must be a mapping")
    allowed_keys = frozenset((*_DIMENSIONS, "spend_micro_units"))
    if not basis:
        raise RepositoryValidationError("usage basis must not be empty when present")
    for key, value in basis.items():
        if key not in allowed_keys:
            raise RepositoryValidationError("unknown usage basis dimension")
        if value not in _USAGE_BASIS_VALUES:
            raise RepositoryValidationError("invalid usage basis value")
    canonical(basis, 1024)


_PHASES = {
    "awaiting_event",
    "waiting",
    "checking",
    "investigating",
    "delegating",
    "waiting_approval",
    "waiting_authorization",
    "budget_exhausted",
    "reconciliation",
    "failed",
}
_TERMINAL = {"stopped", "completed"}
_OPERATION_STATE_KEYS = {"control", "terminal_outcome", "result_reference"}


def plain(value: Any) -> Any:
    if is_dataclass(value):
        result = {f.name: plain(getattr(value, f.name)) for f in fields(value)}
        for model, key in (
            (AssignmentActionIntent, "transient_input"),
            (AssignmentActionOutcome, "result_disposition"),
        ):
            if isinstance(value, model) and result[key] is None:
                result.pop(key)
        return result
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise RepositoryValidationError("timestamp must have a timezone")
        return value.astimezone(UTC).isoformat()
    return value


def canonical(value: Any, maximum: int = 262144) -> str:
    try:
        encoded = json.dumps(
            plain(value), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError) as exc:
        raise RepositoryValidationError("invalid bounded JSON") from exc
    if len(encoded.encode()) > maximum:
        raise RepositoryValidationError("assignment JSON exceeds its bound")
    return encoded


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _text(value, maximum=512):
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > maximum:
        raise RepositoryValidationError("invalid bounded text")
    return value


def _uuid(value):
    try:
        result = uuid.UUID(value)
        if result.version != 4 or str(result) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise RepositoryValidationError("canonical UUID4 required") from exc
    return value


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
        raise RepositoryValidationError("SHA-256 required")
    return value


def _integer(value, minimum=0, maximum=2**53 - 1):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise RepositoryValidationError("integer outside supported bound")
    return value


def _time(value):
    if value is None:
        return None
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise RepositoryValidationError("aware timestamp required")
    return result.astimezone(UTC)


def _now(transaction):
    return transaction.fetch_one("SELECT clock_timestamp() AS now")["now"]


def _guidance_cutoff(value):
    if value is None:
        return None
    try:
        if type(value) is not datetime or value.utcoffset() is None:
            raise ValueError
        # Detach caller's tzinfo before the transaction can wait
        return value.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        raise RepositoryValidationError("aware authority cutoff required") from None


def _conflict(code):
    raise RepositoryConflictError(code, code=code)


def _version(data, revision, epoch):
    _integer(revision, 1)
    _integer(epoch, 1)
    if data["instruction_revision"] != revision or data["control_epoch"] != epoch:
        _conflict("assignment_revision_conflict")


def _state_version(data, expected):
    _integer(expected, 1)
    if data["state_version"] != expected:
        _conflict("assignment_revision_conflict")


def _supported(data):
    if data.get("execution_profile") != "one_shot":
        return True
    operation = data["operation"]
    return (
        operation["version"] in {1, 2}
        and data["checkpoint"].get("schema_version", 1) == 1
        and operation.get("control", {}).get("version", 1) == 1
    )


def _executable(data):
    if data.get("execution_profile") != "one_shot":
        return True
    if not (_supported(data) and data["operation"]["version"] == 2):
        return False
    authority = data["operation"]["authority"]
    return (authority["origin"], authority["reference_kind"]) in (
        ("interactive", "session_incarnation"),
        ("framework", "credential"),
    )


def _require_executable(data):
    if not _executable(data):
        _conflict("assignment_version_unsupported")


def _operation_control(operation):
    return operation.setdefault(
        "control", {"version": 1, "wait": None, "watermarks": {}, "wake_receipts": {}}
    )


def _definition(value):
    return AssignmentDefinition(**value)


def _record(data):
    names = {entry.name for entry in fields(AssignmentRecord)} - {
        "last_completed_generation",
        "execution_profile",
        "operation",
        "harness_capture",
    }
    values = {key: data[key] for key in names}
    values["last_completed_generation"] = data.get("last_completion", {}).get("claim_generation", 0)
    values["execution_profile"] = data.get("execution_profile", "persistent")
    values["operation"] = data.get("operation")
    captured = data.get("harness_capture")
    values["harness_capture"] = None if captured is None else harness_capture_from_value(captured)
    if (values["harness_capture"] is not None
            and values["harness_capture"].owner_id != values["owner_id"]):
        raise RepositoryDataError("assignment harness capture owner is invalid")
    values["definition"] = _definition(values["definition"])
    for key in ("created_at", "updated_at", "next_wake_at"):
        values[key] = _time(values[key])
    return AssignmentRecord(**values)


def _intent(data):
    values = dict(data)
    values["maximum"] = AssignmentResourceAmount(**values["maximum"])
    for key in ("quote_expires_at", "approval_expires_at"):
        values[key] = _time(values[key])
    if values.get("transient_input") is not None:
        values["transient_input"] = _payload_record(
            values["transient_input"], AssignmentTransientInput
        )
    return AssignmentActionIntent(**values)


def _payload_record(value, model):
    value = plain(value)
    if not isinstance(value, dict):
        raise RepositoryValidationError("invalid payload disposition")
    _integer(value.get("version"), 1)
    if value["version"] != 1:
        return value
    try:
        value["references"] = tuple(
            AssignmentInputReference(**item) for item in value["references"]
        )
        return model(**value)
    except (KeyError, TypeError, ValueError):
        raise RepositoryValidationError("invalid payload disposition") from None


def _input_references(references):
    if not isinstance(references, (tuple, list)) or len(references) > 60:
        raise RepositoryValidationError("invalid reconstruction references")
    counts, seen = {}, set()
    for item in references:
        if (
            not isinstance(item, AssignmentInputReference)
            or type(item.kind) is not str
            or item.kind
            not in {
                "source",
                "note",
                "skill",
            }
        ):
            raise RepositoryValidationError("invalid reconstruction reference")
        _text(item.resource_id, 256)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*", item.resource_id):
            raise RepositoryValidationError("invalid reconstruction reference")
        if item.kind == "note":
            _uuid(item.resource_id)
        _integer(item.revision, 1)
        identity = (item.kind, item.resource_id)
        if identity in seen:
            raise RepositoryValidationError("duplicate reconstruction reference")
        seen.add(identity)
        counts[item.kind] = counts.get(item.kind, 0) + 1
        if counts[item.kind] > {"source": 32, "note": 8, "skill": 20}[item.kind]:
            raise RepositoryValidationError("reconstruction reference bound exceeded")
    canonical(references, 8192)


def _payload_identifier(value, maximum):
    _text(value, maximum)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*", value):
        raise RepositoryValidationError("invalid payload identifier")


def _transient_input(value, request, request_digest):
    value = _payload_record(value, AssignmentTransientInput)
    if not isinstance(value, AssignmentTransientInput):
        _conflict("assignment_payload_version_unsupported")
    _payload_identifier(value.binding_key_id, 64)
    _digest(value.payload_binding)
    if (
        value.reconstruction_kind != "model_messages"
        or type(value.source_retention) is not str
        or value.source_retention
        not in {
            "none",
            "operation",
        }
    ):
        raise RepositoryValidationError("invalid reconstruction disposition")
    _input_references(value.references)
    if (
        not isinstance(request, Mapping)
        or request.get("kind") != "model"
        or set(request)
        - {"kind", "model", "provider", "max_output_tokens", "reasoning_effort", "response_format"}
    ):
        raise RepositoryValidationError("transient request requires routing metadata only")
    _integer(request.get("max_output_tokens"), 1, 1_000_000)
    for key in ("model", "provider"):
        if key in request:
            _payload_identifier(request[key], 128)
    if (
        request.get("reasoning_effort") is not None
        and type(request.get("reasoning_effort")) is not str
    ) or request.get("reasoning_effort") not in {
        None,
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    }:
        raise RepositoryValidationError("invalid reasoning disposition")
    if "response_format" in request and request["response_format"] not in (
        {"type": "text"},
        {"type": "json_object"},
    ):
        raise RepositoryValidationError("invalid response disposition")
    if request_digest != value.payload_binding:
        raise RepositoryValidationError("transient payload binding mismatch")
    canonical(value, 12288)
    return value


def _result_disposition(value, result):
    value = _payload_record(value, AssignmentResultDisposition)
    if not isinstance(value, AssignmentResultDisposition):
        _conflict("assignment_payload_version_unsupported")
    if type(value.available) is not bool:
        raise RepositoryValidationError("result availability must be boolean")
    _input_references(value.references)
    if value.binding_key_id is not None:
        _payload_identifier(value.binding_key_id, 64)
    if value.available:
        if value.reason is not None or value.references:
            raise RepositoryValidationError("available result cannot request reacquisition")
    elif (
        type(value.reason) is not str
        or value.reason not in {"retention_discarded", "stale_execution", "reconstruction_required"}
        or result
    ):
        raise RepositoryValidationError("unavailable result must contain no payload bytes")
    canonical(value, 12288)
    return value


def _outcome_projection(outcome):
    value = plain(outcome)
    disposition = value.get("result_disposition")
    if disposition is not None:
        value["result_available"] = disposition["available"]
        if not disposition["available"]:
            value["reacquisition_reason"] = disposition["reason"]
    return value


def _transient_receipt(disposition, evidence_reference):
    if disposition is None or disposition.binding_key_id is None:
        raise RepositoryValidationError("transient result requires a keyed receipt binding")
    if evidence_reference is not None:
        _payload_identifier(evidence_reference, 256)


def _action_record(data):
    return AssignmentActionRecord(
        data["action_id"],
        data["assignment_id"],
        data["owner_id"],
        _intent(data["intent"]),
        data["instruction_revision"],
        data["control_epoch"],
        data["state"],
        data.get("result"),
        tuple(
            {
                k: v
                for k, v in item.items()
                if k
                not in {"dispatch_token", "binding", "assignment_fence", "settlement_signature"}
            }
            for item in data["attempts"]
        ),
        data.get("interactive_proposal_id"),
        any(item.get("dispatch_token") is not None for item in data["attempts"]),
    )


class AssignmentRepository:
    @staticmethod
    def validate_definition(definition: AssignmentDefinition) -> None:
        AssignmentRepository._validate_definition(definition, one_shot=False)

    @staticmethod
    def validate_operation_definition(definition: AssignmentDefinition) -> None:
        AssignmentRepository._validate_definition(definition, one_shot=True)

    @staticmethod
    def _validate_definition(definition, *, one_shot):
        if not isinstance(definition, AssignmentDefinition):
            raise RepositoryValidationError("typed assignment definition required")
        _text(definition.name, 256)
        _text(definition.instructions, 8192)
        if not isinstance(definition.source, Mapping) or not isinstance(definition.limits, Mapping):
            raise RepositoryValidationError("source and limits must be objects")
        canonical(definition.source, 8192)
        if not one_shot and (not definition.source or not 1 <= len(definition.allowed_tools) <= 64):
            raise RepositoryValidationError("source and explicit allowed tools required")
        for values in (definition.allowed_tools, definition.consented_scopes):
            if len(values) > 64 or len(set(values)) != len(values):
                raise RepositoryValidationError("invalid tool/scope set")
            for value in values:
                _text(value, 256)
        if definition.offline_grant_id is not None:
            _uuid(definition.offline_grant_id)
        limits = definition.limits
        if one_shot:
            known_limits = (
                set(_DIMENSIONS)
                | {"daily_" + key for key in _DIMENSIONS}
                | {
                    "max_retries",
                    "max_concurrent_tasks",
                    "max_depth",
                    "max_tasks",
                    "spend_micro_units",
                    "daily_spend_micro_units",
                    "currency",
                }
            )
            if set(limits) - known_limits:
                raise RepositoryValidationError("unknown one-shot limit")
        if not one_shot:
            _integer(limits.get("cadence_seconds"), 60, 31536000)
        elif "cadence_seconds" in limits:
            raise RepositoryValidationError("one-shot work cannot declare recurrence")
        for key, maximum in (
            ("max_retries", 3 if one_shot else 10),
            ("max_concurrent_tasks", 5),
            ("max_depth", 4),
            ("max_tasks", 32),
        ):
            _integer(limits.get(key), 0 if key in {"max_retries", "max_depth"} else 1, maximum)
        for key in _DIMENSIONS:
            minimum = 0 if one_shot and key == "tool_calls" else 1
            _integer(limits.get(key), minimum)
            if not one_shot or "daily_" + key in limits:
                _integer(limits.get("daily_" + key), minimum)
        if limits.get("spend_micro_units") is not None:
            _integer(limits["spend_micro_units"])
            _integer(
                limits.get("daily_spend_micro_units", limits["spend_micro_units"])
                if one_shot
                else limits.get("daily_spend_micro_units")
            )
            _text(limits.get("currency"), 8)
            coverage = definition.cost_quote_coverage
            if not coverage or not coverage.get("quote_digest") or not coverage.get("expires_at"):
                raise RepositoryValidationError("cost_bound_unavailable")
            _digest(coverage["quote_digest"])
            _time(coverage["expires_at"])
        elif limits.get("currency") is not None:
            raise RepositoryValidationError("currency requires an explicit monetary cap")
        canonical(definition, 32768)

    def _load(
        self, query, owner_id, assignment_id, *, lock=False, required=True, allow_unknown=False
    ):
        _text(owner_id)
        _uuid(assignment_id)
        row = query.fetch_one(
            "SELECT * FROM persistent_assignment WHERE id=%s AND owner_user_id=%s"
            + (" FOR UPDATE" if lock else ""),
            (assignment_id, owner_id),
        )
        if row is None:
            if required:
                raise RepositoryNotFoundError("assignment_not_found", code="assignment_not_found")
            return None
        data = self._validated_assignment_row(row, owner_id, assignment_id)
        if not allow_unknown and not _supported(data):
            _conflict("assignment_version_unsupported")
        return data

    def _validated_assignment_row(self, row, owner_id, assignment_id):
        try:
            data = plain(row["data"])
            if (
                data["assignment_id"] != assignment_id
                or data["owner_id"] != owner_id
                or data["state_version"] != row["state_version"]
                or data["lifecycle"] != row["lifecycle"]
                or _time(data["next_wake_at"]) != row["next_wake_at"]
                or _time(data["lease_expires_at"]) != row["lease_expires_at"]
            ):
                raise ValueError
            profile = row.get("execution_profile", "persistent")
            if profile != data.get("execution_profile", "persistent"):
                raise ValueError
            if profile == "one_shot":
                self.validate_operation_definition(_definition(data["definition"]))
                self._validate_operation_state(data["operation"], owner_id)
            elif profile == "persistent":
                self.validate_definition(_definition(data["definition"]))
            else:
                raise ValueError
            if data["phase"] not in _PHASES or not isinstance(data["tasks"], list):
                raise ValueError
            if len(data["tasks"]) > 32 or not isinstance(data["checkpoint"], dict):
                raise ValueError
            if profile == "one_shot":
                _integer(data["checkpoint"].get("schema_version", 1), 1)
            for key in ("instruction_revision", "control_epoch", "state_version"):
                _integer(data[key], 1)
            for bucket in ("spent", "daily", "outstanding"):
                for amount in data["usage"][bucket].values():
                    _integer(amount)
            for task in data["tasks"]:
                AssignmentTask(**task)
            _record(data)
            canonical(data, 262144)
            return data
        except (KeyError, TypeError, ValueError, RepositoryValidationError) as exc:
            raise RepositoryDataError("invalid persisted assignment") from exc

    def bind_guidance_references(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        expected_instruction_revision,
        expected_control_epoch,
        expected_state_version,
        references,
    ):
        return self._bind_guidance_references(
            transaction,
            owner_id=owner_id,
            assignment_id=assignment_id,
            expected_instruction_revision=expected_instruction_revision,
            expected_control_epoch=expected_control_epoch,
            expected_state_version=expected_state_version,
            references=references,
        )

    def bind_selected_input(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        expected_instruction_revision,
        expected_control_epoch,
        expected_state_version,
        envelope,
    ):
        envelope = copy_envelope(envelope)
        return self._bind_guidance_references(
            transaction,
            owner_id=owner_id,
            assignment_id=assignment_id,
            expected_instruction_revision=expected_instruction_revision,
            expected_control_epoch=expected_control_epoch,
            expected_state_version=expected_state_version,
            references=envelope.references,
            envelope=envelope,
        )

    def _bind_guidance_references(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        expected_instruction_revision,
        expected_control_epoch,
        expected_state_version,
        references,
        envelope=None,
    ):
        from astralplane.repositories.guidance_models import GuidanceReference

        if type(references) is not tuple or any(
            type(ref) is not GuidanceReference for ref in references
        ):
            raise RepositoryValidationError("typed guidance references required")
        references = tuple(
            GuidanceReference(ref.kind, ref.resource_id, ref.revision) for ref in references
        )
        if (
            sum(ref.kind == "skill" for ref in references) > 20
            or sum(ref.kind == "note" for ref in references) > 8
            or len({(ref.kind, ref.resource_id) for ref in references}) != len(references)
        ):
            raise RepositoryValidationError("guidance selection exceeds bounds")
        if not self._lock_operation_owner(transaction, owner_id):
            _conflict("assignment_owner_retired")
        data = self._load(transaction, owner_id, assignment_id, lock=True)
        _version(data, expected_instruction_revision, expected_control_epoch)
        _state_version(data, expected_state_version)
        existing = transaction.fetch_all(
            (
                "SELECT * FROM assignment_guidance_reference WHERE owner_id=%s AND assi"
                "gnment_id=%s ORDER BY kind,resource_id"
            ),
            (owner_id, assignment_id),
        )
        requested = sorted((ref.kind, ref.resource_id, ref.revision) for ref in references)
        selection = transaction.fetch_one(
            "SELECT * FROM assignment_guidance_selection WHERE owner_id=%s AND assignment_id=%s",
            (owner_id, assignment_id),
        )
        if selection is not None:
            stored = self._selected_input_snapshot(transaction, data)[0]
            if stored.envelope != envelope:
                _conflict("assignment_guidance_revision_conflict")
            if selection["reference_digest"] != digest(requested):
                _conflict("assignment_guidance_revision_conflict")
            actual = [(row["kind"], str(row["resource_id"]), row["revision"]) for row in existing]
            if actual != requested or any(
                row["instruction_revision"] != data["instruction_revision"] for row in existing
            ):
                _conflict("assignment_guidance_revision_conflict")
            self._assert_guidance_current(transaction, data)
            return _record(data)
        if (
            data["lifecycle"] != "active"
            or data["phase"] != "waiting"
            or data["instruction_revision"] != 1
            or data["claim_generation"] != 0
            or data["claim_token"] is not None
            or data["tasks"]
            or transaction.fetch_one(
                "SELECT id FROM persistent_assignment_action WHERE assignment_id=%s LIMIT 1",
                (assignment_id,),
            )
        ):
            _conflict("assignment_guidance_binding_closed")
        if existing or self._selected_input_snapshot(transaction, data)[0] is not None:
            raise RepositoryDataError("guidance references have no selection header")
        with transaction.savepoint("assignment_guidance_bind"):
            if envelope is not None:
                _require_executable(data)
                self._assert_selected_agent_current(
                    transaction,
                    data,
                    AssignmentSelectedInput(
                        owner_id,
                        assignment_id,
                        data["instruction_revision"],
                        envelope,
                        envelope.references,
                    ),
                    {"invalidated_at": None},
                )
            transaction.execute(
                (
                    "INSERT INTO assignment_guidance_selection(owner_id,assignment_id,instr"
                    "uction_revision,reference_digest,created_at,selected_input) "
                    "VALUES(%s,%s,%s,%s,floor("
                    "extract(epoch FROM clock_timestamp())*1000)::bigint,%s::jsonb)"
                ),
                (
                    owner_id,
                    assignment_id,
                    data["instruction_revision"],
                    digest(requested),
                    None if envelope is None else canonical(asdict(envelope)),
                ),
            )
            for ref in references:
                transaction.execute(
                    (
                        "INSERT INTO assignment_guidance_reference(owner_id,assignment_id,instr"
                        "uction_revision,kind,resource_id,revision) VALUES(%s,%s,%s,%s,%s,%s)"
                    ),
                    (
                        owner_id,
                        assignment_id,
                        data["instruction_revision"],
                        ref.kind,
                        ref.resource_id,
                        ref.revision,
                    ),
                )
            if envelope is not None and envelope.agent is not None:
                agent = envelope.agent
                transaction.execute(
                    "INSERT INTO assignment_selected_agent(owner_id,assignment_id,"
                    "instruction_revision,agent_id,revision_id,definition_digest,kind) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (
                        owner_id,
                        assignment_id,
                        data["instruction_revision"],
                        agent.agent_id,
                        agent.revision_id,
                        agent.definition_digest,
                        agent.kind,
                    ),
                )
            self._assert_guidance_current(transaction, data)
            return self._save(transaction, data)

    def get_selected_input(self, transaction, *, owner_id, assignment_id):
        if not self._lock_operation_owner(transaction, owner_id):
            _conflict("assignment_owner_retired")
        data = self._load(transaction, owner_id, assignment_id, lock=True)
        return self._selected_input_snapshot(transaction, data)[0]

    def assert_selected_input_current(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        expected_instruction_revision,
        expected_control_epoch,
        expected_state_version,
        expected,
        authority_valid_until: datetime | None = None,
    ):
        authority_valid_until = _guidance_cutoff(authority_valid_until)
        if expected is not None:
            if type(expected) is not AssignmentSelectedInput:
                raise RepositoryValidationError("typed selected input snapshot required")
            expected = AssignmentSelectedInput(
                expected.owner_id,
                expected.assignment_id,
                expected.instruction_revision,
                expected.envelope,
                expected.references,
            )
        if not self._lock_operation_owner(transaction, owner_id):
            _conflict("assignment_owner_retired")
        data = self._load(transaction, owner_id, assignment_id, lock=True)
        _version(data, expected_instruction_revision, expected_control_epoch)
        _state_version(data, expected_state_version)
        if self._selected_input_snapshot(transaction, data)[0] != expected:
            _conflict("assignment_guidance_changed")
        self._assert_guidance_current(transaction, data, authority_valid_until)
        return _record(data)

    @staticmethod
    def _selected_input_snapshot(transaction, data):
        from astralplane.repositories.guidance_models import GuidanceReference, integer

        rows = transaction.fetch_all(
            "SELECT * FROM assignment_guidance_reference WHERE owner_id=%s AND "
            "assignment_id=%s ORDER BY kind,resource_id",
            (data["owner_id"], data["assignment_id"]),
        )
        header = transaction.fetch_one(
            "SELECT * FROM assignment_guidance_selection WHERE owner_id=%s AND assignment_id=%s",
            (data["owner_id"], data["assignment_id"]),
        )
        agent = transaction.fetch_one(
            "SELECT * FROM assignment_selected_agent WHERE owner_id=%s AND assignment_id=%s",
            (data["owner_id"], data["assignment_id"]),
        )
        if header is None:
            if rows or agent is not None:
                raise RepositoryDataError("selected references have no header")
            return None, rows, agent
        try:
            refs = tuple(
                GuidanceReference(r["kind"], str(r["resource_id"]), r["revision"]) for r in rows
            )
            if header["reference_digest"] != digest(
                [(r.kind, r.resource_id, r.revision) for r in refs]
            ):
                raise ValueError
            for row in (*rows, *((agent,) if agent is not None else ())):
                integer(row["instruction_revision"], minimum=1)
                if (
                    row["instruction_revision"] != header["instruction_revision"]
                    or type(row["active"]) is not bool
                ):
                    raise ValueError
                if row["invalidated_at"] is not None:
                    integer(row["invalidated_at"], maximum=2**63 - 1)
            envelope = (
                None
                if header["selected_input"] is None
                else decode_envelope(plain(header["selected_input"]))
            )
            reference = (
                None
                if agent is None
                else SelectedAgentReference(
                    agent["agent_id"],
                    str(agent["revision_id"]),
                    agent["definition_digest"],
                    agent["kind"],
                )
            )
            if reference != (None if envelope is None else envelope.agent):
                raise ValueError
            snapshot = AssignmentSelectedInput(
                data["owner_id"],
                data["assignment_id"],
                header["instruction_revision"],
                envelope,
                refs,
            )
            return snapshot, rows, agent
        except (KeyError, TypeError, ValueError, RepositoryValidationError) as exc:
            raise RepositoryDataError("invalid stored selected input") from exc

    @staticmethod
    def _assert_selected_agent_current(transaction, data, snapshot, agent):
        if snapshot is None or snapshot.envelope is None or snapshot.envelope.agent is None:
            return
        ref = snapshot.envelope.agent
        current = transaction.fetch_one(
            "SELECT a.agent_kind,a.status,a.deleted_at,a.selected_definition_revision_id,"
            "r.revision_kind,r.state,r.compatibility_state,r.definition_digest "
            "FROM user_agent a JOIN user_agent_revision r ON r.agent_id=a.agent_id "
            "AND r.owner_user_id=a.owner_user_id AND r.revision_id=%s "
            "WHERE a.owner_user_id=%s AND a.agent_id=%s",
            (ref.revision_id, data["owner_id"], ref.agent_id),
        )
        if (
            agent["invalidated_at"] is not None
            or current is None
            or current["agent_kind"] != "declarative"
            or current["status"] != "active"
            or current["deleted_at"] is not None
            or str(current["selected_definition_revision_id"]) != ref.revision_id
            or current["revision_kind"] != "declarative"
            or current["state"] != "definition"
            or current["compatibility_state"] != "declarative"
            or current["definition_digest"] != ref.definition_digest
        ):
            _conflict("assignment_guidance_changed")

    def assert_guidance_current(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        expected_instruction_revision,
        expected_control_epoch,
        expected_state_version,
        authority_valid_until: datetime | None = None,
    ):
        authority_valid_until = _guidance_cutoff(authority_valid_until)
        if not self._lock_operation_owner(transaction, owner_id):
            _conflict("assignment_owner_retired")
        data = self._load(transaction, owner_id, assignment_id, lock=True)
        _version(data, expected_instruction_revision, expected_control_epoch)
        _state_version(data, expected_state_version)
        self._assert_guidance_current(transaction, data, authority_valid_until)
        return _record(data)

    @staticmethod
    def _assert_guidance_current(transaction, data, authority_valid_until=None):
        from astralplane.repositories.guidance import ExplicitNotesRepository, SkillsRepository
        from astralplane.repositories.guidance_models import ExplicitNoteRecord, GuidanceReference

        snapshot, rows, agent = AssignmentRepository._selected_input_snapshot(transaction, data)
        if snapshot is not None and snapshot.instruction_revision != data["instruction_revision"]:
            _conflict("assignment_guidance_changed")
        AssignmentRepository._assert_selected_agent_current(transaction, data, snapshot, agent)
        expiries = []
        for row in rows:
            ref = GuidanceReference(row["kind"], str(row["resource_id"]), row["revision"])
            if (
                row["invalidated_at"] is not None
                or row["instruction_revision"] != data["instruction_revision"]
            ):
                _conflict("assignment_guidance_changed")
            if ref.kind == "skill":
                current = SkillsRepository().get(
                    transaction, owner_id=data["owner_id"], skill_id=ref.resource_id
                )
                valid = current is not None and current.enabled and current.revision == ref.revision
            else:
                current = ExplicitNotesRepository().get_explicit_note(
                    transaction,
                    owner_id=data["owner_id"],
                    note_id=ref.resource_id,
                    include_disabled=False,
                )
                valid = type(current) is ExplicitNoteRecord and current.revision == ref.revision
                if valid and current.expires_at is not None:
                    expiries.append(current.expires_at)
            if not valid:
                _conflict("assignment_guidance_changed")
        if authority_valid_until is not None:
            observed_at = _now(transaction)
            observed_ms = (observed_at - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(
                milliseconds=1
            )
            if observed_at >= authority_valid_until or (expiries and min(expiries) <= observed_ms):
                _conflict("assignment_guidance_changed")
        elif expiries:
            from astralplane.repositories.guidance import _clock

            if min(expiries) <= _clock(transaction):
                _conflict("assignment_guidance_changed")

    def _lock_guidance_dependants(self, transaction, owner_id, kind, resource_id):
        rows = transaction.fetch_all(
            (
                "SELECT a.* FROM persistent_assignment a WHERE a.owner_user_id=%s AND a"
                ".lifecycle IN ('active','paused') AND EXISTS(SELECT 1 FROM assignment_"
                "guidance_reference r WHERE r.owner_id=a.owner_user_id AND r.assignment"
                "_id=a.id AND r.kind=%s AND r.resource_id=%s AND r.active AND r.invalid"
                "ated_at IS NULL) ORDER BY a.id FOR UPDATE"
            ),
            (owner_id, kind, resource_id),
        )
        identities = [str(row["id"]) for row in rows]
        if identities:
            transaction.fetch_all(
                (
                    "SELECT id FROM persistent_assignment_action WHERE owner_user_id=%s AND"
                    " assignment_id=ANY(%s::uuid[]) ORDER BY id FOR UPDATE"
                ),
                (owner_id, identities),
            )
        return rows

    def _invalidate_guidance_dependants(self, transaction, owner_id, kind, resource_id):
        rows = self._lock_guidance_dependants(transaction, owner_id, kind, resource_id)
        self._invalidate_guidance_assignments(transaction, rows, owner_id)
        transaction.execute(
            "UPDATE assignment_guidance_reference SET invalidated_at=floor(extract("
            "epoch FROM clock_timestamp())*1000)::bigint WHERE owner_id=%s AND kind"
            "=%s AND resource_id=%s AND active AND invalidated_at IS NULL",
            (owner_id, kind, resource_id),
        )

    def _lock_selected_agent_dependants(self, transaction, owner_id):
        rows = transaction.fetch_all(
            "SELECT a.* FROM persistent_assignment a WHERE a.owner_user_id=%s "
            "AND a.lifecycle IN ('active','paused') AND EXISTS(SELECT 1 FROM "
            "assignment_selected_agent r WHERE r.owner_id=a.owner_user_id "
            "AND r.assignment_id=a.id AND r.active AND r.invalidated_at IS NULL) "
            "ORDER BY a.id FOR UPDATE",
            (owner_id,),
        )
        if rows:
            transaction.fetch_all(
                "SELECT id FROM persistent_assignment_action WHERE owner_user_id=%s "
                "AND assignment_id=ANY(%s::uuid[]) ORDER BY id FOR UPDATE",
                (owner_id, [str(row["id"]) for row in rows]),
            )

    def _invalidate_selected_agent_dependants(self, transaction, owner_id, agent_id):
        rows = transaction.fetch_all(
            "SELECT a.* FROM persistent_assignment a JOIN assignment_selected_agent r "
            "ON r.owner_id=a.owner_user_id AND r.assignment_id=a.id WHERE r.owner_id=%s "
            "AND r.agent_id=%s AND r.active AND r.invalidated_at IS NULL "
            "AND a.lifecycle IN ('active','paused') ORDER BY a.id",
            (owner_id, agent_id),
        )
        self._invalidate_guidance_assignments(transaction, rows, owner_id)
        transaction.execute(
            "UPDATE assignment_selected_agent SET invalidated_at=floor(extract("
            "epoch FROM clock_timestamp())*1000)::bigint WHERE owner_id=%s "
            "AND agent_id=%s AND active AND invalidated_at IS NULL",
            (owner_id, agent_id),
        )

    def _invalidate_guidance_assignments(self, transaction, rows, owner_id):
        for row in rows:
            data = self._validated_assignment_row(row, owner_id, str(row["id"]))
            if not _supported(data):
                continue
            data["control_epoch"] += 1
            self._clear_claim(data)
            _, begun = self._invalidate_actions(transaction, data, conservative=True)
            for task in data["tasks"]:
                if task["state"] in {"pending", "running"}:
                    task["task_generation"] += 1
                    task["state"] = "pending"
            phase = data["phase"]
            if phase not in {
                "waiting_authorization",
                "awaiting_event",
                "waiting_approval",
                "budget_exhausted",
                "reconciliation",
            }:
                phase = "reconciliation" if begun else "waiting"
            data.update(
                lifecycle="paused",
                phase=phase,
                safe_error_code="guidance_changed",
                next_wake_at=None,
                next_retry_at=None,
            )
            self._save(transaction, data)

    def _save(self, transaction, data):
        old_version = data["state_version"]
        data["state_version"] += 1
        data["updated_at"] = plain(_now(transaction))
        row = transaction.fetch_one(
            "UPDATE persistent_assignment SET data=%s::jsonb,lifecycle=%s,next_wake_at=%s,"
            "lease_expires_at=%s,state_version=%s WHERE id=%s AND owner_user_id=%s "
            "AND state_version=%s RETURNING id",
            (
                canonical(data),
                data["lifecycle"],
                data["next_wake_at"],
                data["lease_expires_at"],
                data["state_version"],
                data["assignment_id"],
                data["owner_id"],
                old_version,
            ),
        )
        if row is None:
            _conflict("assignment_revision_conflict")
        if data["lifecycle"] in _TERMINAL:
            transaction.execute(
                "UPDATE assignment_guidance_reference SET active=FALSE WHERE owner_id=%s "
                "AND assignment_id=%s AND active",
                (data["owner_id"], data["assignment_id"]),
            )
            transaction.execute(
                "UPDATE assignment_selected_agent SET active=FALSE WHERE owner_id=%s "
                "AND assignment_id=%s AND active",
                (data["owner_id"], data["assignment_id"]),
            )
        return _record(data)

    def _fenced(self, transaction, fence, *, action_id=None):
        data = self._load(transaction, fence.owner_id, fence.assignment_id, lock=True)
        _require_executable(data)
        _version(data, fence.instruction_revision, fence.control_epoch)
        if (
            data["lifecycle"] != "active"
            or data["claim_token"] != fence.claim_token
            or data["claim_generation"] != fence.claim_generation
            or _time(data["lease_expires_at"]) is None
            or _time(data["lease_expires_at"]) <= _now(transaction)
        ):
            _conflict("assignment_claim_stale")
        if data.get("approved_action_id") and data["approved_action_id"] != action_id:
            _conflict("assignment_action_claim_restricted")
        if data.get("execution_profile") == "one_shot":
            self._check_operation_time(
                transaction, self._operation_spec(data["operation"], fence.owner_id)
            )
        return data

    @staticmethod
    def _clear_claim(data):
        data.update(
            claim_token=None,
            lease_expires_at=None,
            claimed_by=None,
            operation_binding=None,
            approved_action_id=None,
        )

    def _claim(self, transaction, data, worker_id, lease_seconds, action_id=None):
        self._assert_guidance_current(transaction, data)
        _text(worker_id, 128)
        _integer(lease_seconds, 5, 60)
        previous = data.get("operation_binding")
        data.update(
            claim_generation=data["claim_generation"] + 1,
            claim_token=str(uuid.uuid4()),
            lease_expires_at=plain(_now(transaction) + timedelta(seconds=lease_seconds)),
            claimed_by=worker_id,
            operation_binding=None,
            approved_action_id=action_id,
            phase="checking" if action_id is None else "waiting_approval",
        )
        data["claimed_wake_generation"] = data["wake_generation"]
        record = self._save(transaction, data)
        return self._claim_record(data, record, previous)

    @staticmethod
    def _claim_record(data, record=None, previous=None):
        return AssignmentClaim(
            record or _record(data),
            AssignmentFence(
                data["assignment_id"],
                data["owner_id"],
                data["instruction_revision"],
                data["control_epoch"],
                data["claim_generation"],
                data["claim_token"],
            ),
            _time(data["lease_expires_at"]),
            previous,
            data.get("approved_action_id"),
        )

    def create_assignment(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        submission_id,
        submission_digest,
        definition,
        max_owned_assignments=25,
        max_retained_assignments=256,
        harness_selection=None,
        harness_capture=None,
    ):
        self.validate_definition(definition)
        _text(owner_id)
        _uuid(assignment_id)
        _uuid(submission_id)
        _digest(submission_digest)
        _integer(max_owned_assignments, 1, 25)
        _integer(max_retained_assignments, 1, 256)
        transaction.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", (owner_id,))
        retired = transaction.fetch_one(
            "SELECT state FROM astralplane_blob_owner_state WHERE owner_id=%s FOR UPDATE",
            (owner_id,),
        )
        if retired and retired["state"] != "active":
            _conflict("assignment_owner_retired")
        replay = transaction.fetch_one(
            "SELECT id,submission_digest,data FROM persistent_assignment "
            "WHERE owner_user_id=%s AND submission_id=%s AND execution_profile='persistent'",
            (owner_id, submission_id),
        )
        if replay:
            if replay["submission_digest"] != submission_digest or replay["data"][
                "initial_definition_digest"
            ] != digest(definition):
                _conflict("assignment_idempotency_conflict")
            return self.get_assignment(
                transaction, owner_id=owner_id, assignment_id=str(replay["id"])
            )
        assert_harness_admission(transaction, owner_id=owner_id,
            selection=harness_selection, capture=harness_capture)
        counts = transaction.fetch_one(
            "SELECT count(*) AS total,count(*) FILTER(WHERE lifecycle IN ('active','paused')) "
            "AS active FROM persistent_assignment WHERE owner_user_id=%s "
            "AND execution_profile='persistent'",
            (owner_id,),
        )
        if counts["total"] >= max_retained_assignments or counts["active"] >= max_owned_assignments:
            _conflict("assignment_capacity_exhausted")
        self._validate_references(transaction, owner_id, definition)
        return self._initialize_assignment(
            transaction,
            owner_id=owner_id,
            assignment_id=assignment_id,
            submission_id=submission_id,
            submission_digest=submission_digest,
            definition=definition,
            harness_capture=harness_capture,
        )

    def _initialize_assignment(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        submission_id,
        submission_digest,
        definition,
        operation=None,
        harness_capture=None,
    ):
        now = plain(_now(transaction))
        data = dict(
            assignment_id=assignment_id,
            owner_id=owner_id,
            submission_id=submission_id,
            submission_digest=submission_digest,
            definition=plain(definition),
            initial_definition_digest=digest(definition),
            instruction_revision=1,
            control_epoch=1,
            state_version=1,
            lifecycle="active",
            phase="waiting",
            next_wake_at=now,
            wake_reason="created",
            wake_generation=1,
            checkpoint={"schema_version": 1},
            tasks=[],
            usage={
                "spent": {},
                "daily": {},
                "outstanding": {},
                "day": now[:10],
                "money_status": "unknown",
            },
            created_at=now,
            updated_at=now,
            safe_error_code=None,
            claim_generation=0,
            claim_token=None,
            lease_expires_at=None,
            claimed_by=None,
            operation_binding=None,
            approved_action_id=None,
            controls={},
            source_batches={},
            plans={},
            claimed_wake_generation=0,
            last_check_at=None,
            next_retry_at=None,
            consecutive_failures=0,
            activity_sequence=0,
        )
        if harness_capture is not None:
            data["harness_capture"] = harness_capture.to_dict()
        if operation is not None:
            data.update(execution_profile="one_shot", operation=plain(operation))
        row = transaction.fetch_one(
            "INSERT INTO persistent_assignment(id,owner_user_id,submission_id,submission_digest,"
            "lifecycle,next_wake_at,state_version,data,execution_profile) "
            "VALUES(%s,%s,%s,%s,'active',%s,1,%s::jsonb,%s) "
            "ON CONFLICT DO NOTHING RETURNING id",
            (
                assignment_id,
                owner_id,
                submission_id,
                submission_digest,
                now,
                canonical(data),
                "one_shot" if operation is not None else "persistent",
            ),
        )
        if row is None:
            _conflict("assignment_idempotency_conflict")
        return _record(data)

    @staticmethod
    def _operation_spec(value, owner_id):
        try:
            if isinstance(value, Mapping):
                value = {k: v for k, v in value.items() if k not in _OPERATION_STATE_KEYS}
                value["authority"] = AssignmentOperationAuthority(**value["authority"])
                value = AssignmentOperationSpec(**value)
            if not isinstance(value, AssignmentOperationSpec) or not isinstance(
                value.authority, AssignmentOperationAuthority
            ):
                raise ValueError
            authority = value.authority
            if (
                type(value.version) is not int
                or value.version not in {1, 2}
                or value.kind not in {"chat", "research"}
            ):
                raise ValueError
            if (
                value.source_retention not in {"none", "operation"}
                or authority.owner_id != owner_id
            ):
                raise ValueError
            kinds = {
                "interactive": {
                    "session" if value.version == 1 else "session_incarnation",
                    "delegation",
                },
                "framework": {"credential"},
                "scheduled": {"offline_grant"},
            }
            if authority.reference_kind not in kinds.get(authority.origin, set()):
                raise ValueError
            _text(authority.reference_id, 256)
            if authority.reference_kind == "session_incarnation":
                _uuid(authority.reference_id)
            _text(authority.owner_id)
            if _time(authority.expires_at) is None or _time(value.deadline_at) is None:
                raise ValueError
            canonical(value, 4096)
            return value
        except (KeyError, TypeError, ValueError) as exc:
            raise RepositoryValidationError(
                "invalid one-shot authority reference or profile"
            ) from exc

    def _validate_operation_state(self, operation, owner_id):
        if not isinstance(operation, dict):
            raise RepositoryValidationError("operation object required")
        _integer(operation.get("version"), 1)
        canonical(operation, 73728)
        if operation["version"] not in {1, 2}:
            return
        self._operation_spec(operation, owner_id)
        outcome = operation.get("terminal_outcome")
        if outcome is not None and outcome not in {"completed", "failed", "cancelled"}:
            raise RepositoryValidationError("invalid terminal outcome")
        if operation.get("result_reference") is not None:
            _text(operation["result_reference"], 512)
        control = operation.get("control")
        if control is None:
            if "control" in operation:
                raise RepositoryValidationError("control object required")
            return
        if not isinstance(control, dict):
            raise RepositoryValidationError("control object required")
        _integer(control.get("version"), 1)
        canonical(control, 65536)
        if control["version"] != 1:
            return
        if set(control) != {"version", "wait", "watermarks", "wake_receipts"}:
            raise RepositoryValidationError("invalid operation control fields")
        wait = control["wait"]
        if wait is not None:
            if not isinstance(wait, dict) or set(wait) != {"event_key", "source_revision"}:
                raise RepositoryValidationError("invalid event wait")
            _text(wait["event_key"], 128)
            _integer(wait["source_revision"])
        watermarks, receipts = control["watermarks"], control["wake_receipts"]
        if not isinstance(watermarks, dict) or len(watermarks) > 64:
            raise RepositoryValidationError("invalid event watermarks")
        for key, revision in watermarks.items():
            _text(key, 128)
            _integer(revision)
        if not isinstance(receipts, dict) or len(receipts) > 128:
            raise RepositoryValidationError("invalid wake receipts")
        for event_id, signature in receipts.items():
            _text(event_id, 128)
            _digest(signature)

    @staticmethod
    def _check_operation_time(transaction, operation):
        now = _now(transaction)
        if _time(operation.authority.expires_at) <= now:
            _conflict("assignment_authorization_unavailable")
        if _time(operation.deadline_at) <= now:
            _conflict("assignment_deadline_exceeded")

    def get_operation_receipt(
        self, query, *, owner_id, origin_namespace, caller_key, command_digest, credential_id=None
    ):
        _text(owner_id)
        _text(origin_namespace, 64)
        _text(caller_key, 256)
        _digest(command_digest)
        if credential_id is not None:
            _text(credential_id, 256)
        row = query.fetch_one(
            "SELECT r.assignment_id,r.command_digest,r.credential_id,r.live_assignment_id,a.* "
            "FROM assignment_operation_receipt r LEFT JOIN persistent_assignment a "
            "ON a.id=r.live_assignment_id AND a.owner_user_id=r.owner_id "
            "WHERE r.owner_id=%s AND r.origin_namespace=%s AND r.caller_key=%s",
            (owner_id, origin_namespace, caller_key),
        )
        if row is None:
            return None
        if row["command_digest"] != command_digest or row["credential_id"] != credential_id:
            _conflict("assignment_idempotency_conflict")
        if row["live_assignment_id"] is None or row["data"] is None:
            _conflict("assignment_operation_deleted")
        # Reuses this row; a fresh ID lookup risks a reused UUID
        return _record(self._validated_assignment_row(row, owner_id, str(row["assignment_id"])))

    def create_operation(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        origin_namespace,
        caller_key,
        command_digest,
        definition,
        operation,
        credential_id=None,
        authority=None,
        max_owned_operations=25,
        max_retained_operations=256,
        max_retained_receipts=4096,
        harness_selection=None,
        harness_capture=None,
    ):
        with transaction.savepoint("operation_create_" + uuid.uuid4().hex):
            _text(owner_id)
            _uuid(assignment_id)
            _integer(max_owned_operations, 1, 25)
            _integer(max_retained_operations, 1, 256)
            _integer(max_retained_receipts, 1, 4096)
            transaction.fetch_one(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", (owner_id,)
            )
            retired = transaction.fetch_one(
                "SELECT state FROM astralplane_blob_owner_state WHERE owner_id=%s FOR UPDATE",
                (owner_id,),
            )
            if retired and retired["state"] != "active":
                _conflict("assignment_owner_retired")
            replay = self.get_operation_receipt(
                transaction,
                owner_id=owner_id,
                origin_namespace=origin_namespace,
                caller_key=caller_key,
                command_digest=command_digest,
                credential_id=credential_id,
            )
            if replay is not None:
                return replay
            assert_harness_admission(transaction, owner_id=owner_id,
                selection=harness_selection, capture=harness_capture)
            self.validate_operation_definition(definition)
            if isinstance(operation, Mapping) and set(operation) & _OPERATION_STATE_KEYS:
                raise RepositoryValidationError("operation control is repository-owned")
            operation = self._operation_spec(operation, owner_id)
            if operation.version != 2:
                _conflict("assignment_version_unsupported")
            if (
                operation.authority.reference_id
                if operation.authority.origin == "framework"
                else None
            ) != credential_id:
                raise RepositoryValidationError("framework credential reference mismatch")
            self._assert_creation_authority(transaction, owner_id, definition, operation, authority)
            self._check_operation_time(transaction, operation)
            if _time(operation.deadline_at) > _now(transaction) + timedelta(days=1):
                raise RepositoryValidationError("one-shot deadline exceeds one day")
            if operation.kind == "research" and not definition.source:
                raise RepositoryValidationError("research requires a source plan")
            unattended = operation.authority.origin == "scheduled"
            if unattended or definition.offline_grant_id is not None:
                if unattended and definition.offline_grant_id != operation.authority.reference_id:
                    _conflict("assignment_authorization_unavailable")
                self._validate_references(transaction, owner_id, definition)
            else:
                self._validate_non_grant_references(transaction, owner_id, definition)
            counts = transaction.fetch_one(
                "SELECT count(*) AS total,count(*) FILTER(WHERE lifecycle IN ('active','paused')) "
                "AS active "
                "FROM persistent_assignment WHERE owner_user_id=%s "
                "AND execution_profile='one_shot'",
                (owner_id,),
            )
            if (
                counts["total"] >= max_retained_operations
                or counts["active"] >= max_owned_operations
            ):
                _conflict("assignment_capacity_exhausted")
            receipt_count = transaction.fetch_one(
                "SELECT count(*) AS total FROM assignment_operation_receipt WHERE owner_id=%s",
                (owner_id,),
            )["total"]
            if receipt_count >= max_retained_receipts:
                _conflict("assignment_history_capacity_exhausted")
            record = self._initialize_assignment(
                transaction,
                owner_id=owner_id,
                assignment_id=assignment_id,
                submission_id=str(uuid.uuid4()),
                submission_digest=command_digest,
                definition=definition,
                operation=operation,
                harness_capture=harness_capture,
            )
            transaction.execute(
                "INSERT INTO assignment_operation_receipt(owner_id,origin_namespace,caller_key,"
                "command_digest,credential_id,assignment_id,live_assignment_id,created_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,clock_timestamp())",
                (
                    owner_id,
                    origin_namespace,
                    caller_key,
                    command_digest,
                    credential_id,
                    assignment_id,
                    assignment_id,
                ),
            )
            self._assert_creation_authority(transaction, owner_id, definition, operation, authority)
            self._check_operation_time(transaction, operation)
            return record

    def _assert_creation_authority(self, transaction, owner_id, definition, operation, authority):
        origin = operation.authority.origin
        if origin not in {"interactive", "framework"}:
            return
        data = {
            "owner_id": owner_id,
            "execution_profile": "one_shot",
            "operation": plain(operation),
            "definition": plain(definition),
            "checkpoint": {},
        }
        if not self._lock_execution_authority(transaction, data, authority):
            _conflict("assignment_authorization_unavailable")
        elapsed = _time(operation.authority.expires_at) - datetime(1970, 1, 1, tzinfo=UTC)
        expiry_microseconds = (
            elapsed.days * 86400 + elapsed.seconds
        ) * 1_000_000 + elapsed.microseconds
        if origin == "interactive":
            bound_microseconds = authority.credential.hard_expires_at * 1_000_000
        else:
            bound_microseconds = authority.credential.expires_at * 1_000_000
        if expiry_microseconds > bound_microseconds:
            _conflict("assignment_authorization_unavailable")

    @staticmethod
    def _validate_references(transaction, owner_id, definition):
        if definition.offline_grant_id is not None:
            row = transaction.fetch_one(
                "SELECT id FROM user_offline_grant WHERE id=%s AND user_id=%s "
                "AND revoked_at IS NULL AND expires_at > "
                "extract(epoch FROM clock_timestamp())*1000",
                (definition.offline_grant_id, owner_id),
            )
            if row is None:
                _conflict("assignment_authorization_unavailable")
        else:
            _conflict("assignment_authorization_unavailable")
        AssignmentRepository._validate_non_grant_references(transaction, owner_id, definition)

    @staticmethod
    def _validate_non_grant_references(transaction, owner_id, definition):
        if definition.conversation_id is not None:
            row = transaction.fetch_one(
                "SELECT id FROM chats WHERE id=%s AND user_id=%s",
                (definition.conversation_id, owner_id),
            )
            if row is None:
                _conflict("assignment_conversation_not_owned")
        coverage = definition.cost_quote_coverage
        if definition.limits.get("spend_micro_units") is not None and _time(
            coverage["expires_at"]
        ) <= _now(transaction):
            _conflict("assignment_cost_bound_unavailable")

    def get_assignment(self, query, *, owner_id, assignment_id):
        data = self._load(query, owner_id, assignment_id, required=False, allow_unknown=True)
        return _record(data) if data else None

    def get_operation(self, query, *, owner_id, assignment_id, lock: bool = False):
        if not isinstance(lock, bool):
            raise RepositoryValidationError("lock must be a boolean")
        if lock:
            self._lock_operation_owner(query, owner_id)
        data = self._load(
            query, owner_id, assignment_id, lock=lock, required=False, allow_unknown=True
        )
        if data is None or data.get("execution_profile") != "one_shot":
            return None
        return self._operation_read(data)

    @staticmethod
    def _operation_read(data):
        supported = _supported(data)
        operation = data["operation"]
        terminal = operation.get("terminal_outcome") if supported else None
        if data["lifecycle"] == "stopped":
            disposition = "cancelled"
        elif not supported:
            disposition = "unsupported_version"
        elif data["lifecycle"] == "paused":
            disposition = "paused"
        elif data["lifecycle"] == "completed":
            disposition = terminal or "completed"
        else:
            disposition = {
                "awaiting_event": "awaiting_event",
                "waiting_approval": "awaiting_approval",
                "waiting_authorization": "awaiting_authority",
                "budget_exhausted": "budget_blocked",
                "reconciliation": "reconciliation_required",
                "failed": "retry_eligible" if data["next_wake_at"] else "failed",
            }.get(data["phase"], "active" if data["claim_token"] else "queued")
        return AssignmentOperationRead(
            _record(data),
            disposition,
            _executable(data),
            operation.get("result_reference") if supported else None,
            terminal,
        )

    def list_operations(self, query, *, owner_id, limit=50, after_id=None):
        _text(owner_id)
        _integer(limit, 1, 100)
        if after_id is not None:
            _uuid(after_id)
        rows = query.fetch_all(
            "SELECT * FROM persistent_assignment WHERE owner_user_id=%s "
            "AND execution_profile='one_shot' "
            "AND (%s::uuid IS NULL OR id>%s::uuid) ORDER BY id LIMIT %s",
            (owner_id, after_id, after_id, limit),
        )
        return tuple(
            self._operation_read(self._validated_assignment_row(row, owner_id, str(row["id"])))
            for row in rows
        )

    def get_submission_receipt(
        self, query, *, owner_id, assignment_id, submission_id, submission_digest, command
    ):
        _uuid(submission_id)
        _digest(submission_digest)
        data = self._load(
            query, owner_id, assignment_id, required=False, allow_unknown=command == "stop"
        )
        if data is None:
            return None
        if data["submission_id"] == submission_id:
            if command != "create" or data["submission_digest"] != submission_digest:
                _conflict("assignment_idempotency_conflict")
            return _record(data)
        receipt = data["controls"].get(submission_id)
        if receipt is not None:
            if receipt["command"] != command or receipt["submission_digest"] != submission_digest:
                _conflict("assignment_idempotency_conflict")
            return _record(data)
        receipt = data["controls"].get("check:" + submission_id)
        if receipt is not None:
            if command != "run-now" or receipt != submission_digest:
                _conflict("assignment_idempotency_conflict")
            return _record(data)
        return None

    def list_owner_event_waits(
        self,
        query,
        *,
        owner_id,
        event_keys=None,
        event_key_prefix=None,
        limit=50,
        after_id=None,
        include_terminal=True,
    ):
        _text(owner_id)
        _integer(limit, 1, 100)
        if type(include_terminal) is not bool:
            raise RepositoryValidationError("include_terminal must be a boolean")
        if after_id is not None:
            _uuid(after_id)
        if (event_keys is None) == (event_key_prefix is None):
            raise RepositoryValidationError("one explicit event filter is required")
        if event_keys is not None:
            if type(event_keys) is not tuple:
                raise RepositoryValidationError("bounded event keys must be a tuple")
            _integer(len(event_keys), 1, 64)
            for key in event_keys:
                _text(key, 128)
            predicate = "data #>> '{operation,control,wait,event_key}' = ANY(%s)"
            parameters = (list(event_keys),)
        else:
            _text(event_key_prefix, 128)
            predicate = "left(data #>> '{operation,control,wait,event_key}',length(%s))=%s"
            parameters = (event_key_prefix, event_key_prefix)
        if not include_terminal:
            predicate += " AND lifecycle IN ('active','paused')"
        rows = query.fetch_all(
            "SELECT * FROM persistent_assignment WHERE owner_user_id=%s "
            "AND execution_profile='one_shot' AND "
            + predicate
            + " AND (%s::uuid IS NULL OR id>%s::uuid) ORDER BY id LIMIT %s",
            (owner_id, *parameters, after_id, after_id, limit),
        )
        return tuple(
            self._operation_read(self._validated_assignment_row(row, owner_id, str(row["id"])))
            for row in rows
        )

    def list_assignments(self, query, *, owner_id, limit=50, after_id=None):
        _text(owner_id)
        _integer(limit, 1, 100)
        if after_id is not None:
            _uuid(after_id)
        rows = query.fetch_all(
            "SELECT id FROM persistent_assignment WHERE owner_user_id=%s "
            "AND execution_profile='persistent' "
            "AND (%s::uuid IS NULL OR id>%s::uuid) ORDER BY id LIMIT %s",
            (owner_id, after_id, after_id, limit),
        )
        return tuple(
            self.get_assignment(query, owner_id=owner_id, assignment_id=str(row["id"]))
            for row in rows
        )

    def apply_control(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        expected_instruction_revision,
        expected_control_epoch,
        submission_id,
        submission_digest,
        control,
        replacement=None,
        expected_state_version=None,
    ):
        _uuid(submission_id)
        _digest(submission_digest)
        control = AssignmentControl(control)
        owner_active = True
        if control in {AssignmentControl.RESUME, AssignmentControl.REVISE}:
            owner_active = self._lock_operation_owner(transaction, owner_id)
        data = self._load(
            transaction,
            owner_id,
            assignment_id,
            lock=True,
            allow_unknown=control == AssignmentControl.STOP,
        )
        one_shot = data.get("execution_profile") == "one_shot"
        signature_values = [
            str(control),
            replacement,
            expected_instruction_revision,
            expected_control_epoch,
            submission_digest,
        ]
        if one_shot:
            _integer(expected_state_version, 1)
            signature_values.append(expected_state_version)
        request = digest(signature_values)
        old = data["controls"].get(submission_id)
        if old:
            accepted_signatures = {request}
            if one_shot:
                # Old receipts predate state_version, so accept both forms
                accepted_signatures.add(digest(signature_values[:-1]))
            if old["signature"] not in accepted_signatures:
                _conflict("assignment_idempotency_conflict")
            return AssignmentControlResult(_record(data), False)
        _version(data, expected_instruction_revision, expected_control_epoch)
        if one_shot:
            _state_version(data, expected_state_version)
            if not owner_active:
                _conflict("assignment_owner_retired")
        if data["lifecycle"] in _TERMINAL:
            if control == AssignmentControl.STOP and data["lifecycle"] == "stopped":
                return AssignmentControlResult(_record(data), False)
            _conflict("assignment_not_active")
        if len(data["controls"]) >= 256:
            data["controls"].pop(next(iter(data["controls"])))
        if control == AssignmentControl.REVISE:
            self._assert_guidance_current(transaction, data)
            if replacement is None:
                raise RepositoryValidationError("replacement definition required")
            if one_shot:
                self.validate_operation_definition(replacement)
                self._validate_operation_continuation(transaction, data, replacement)
            else:
                self.validate_definition(replacement)
                self._validate_references(transaction, owner_id, replacement)
            old_limits = data["definition"]["limits"]
            if old_limits.get("currency") != replacement.limits.get("currency") and (
                any(data["usage"]["spent"].values()) or any(data["usage"]["outstanding"].values())
            ):
                _conflict(
                    "assignment_prior_cost_unknown"
                    if old_limits.get("currency") is None
                    else "assignment_currency_change_invalid"
                )
            data["definition"] = plain(replacement)
            data["instruction_revision"] += 1
            for key in (
                "cursor",
                "source_configuration_digest",
                "last_batch_key",
                "last_checked_at",
                "last_finding",
                "last_observation",
            ):
                data["checkpoint"].pop(key, None)
            data["source_batches"] = {}
            for task in data["tasks"]:
                self._activity(
                    transaction,
                    data,
                    AssignmentActivityRecord(
                        f"revision:{expected_instruction_revision}:task:{task['task_id']}",
                        "task_superseded",
                        task["title"],
                        task["bounded_result"] or "",
                        {
                            "task_id": task["task_id"],
                            "instruction_revision": expected_instruction_revision,
                            "prior_state": task["state"],
                            "result_digest": task["result_digest"],
                            "provenance": task["provenance"],
                        },
                    ),
                )
                task["provenance"]["superseded"] = {
                    "prior_state": task["state"],
                    "by_instruction_revision": data["instruction_revision"],
                }
                task["state"] = "superseded"
                task["task_generation"] += 1
            for row in transaction.fetch_all(
                "SELECT id,data FROM persistent_assignment_event WHERE assignment_id=%s "
                "AND state IN ('pending','processing','failed','reconciliation') FOR UPDATE",
                (assignment_id,),
            ):
                event = plain(row["data"])
                event.update(
                    disposition="superseded",
                    result_digest=digest(
                        [
                            "instruction_superseded",
                            expected_instruction_revision,
                            data["instruction_revision"],
                        ]
                    ),
                )
                transaction.execute(
                    "UPDATE persistent_assignment_event SET state='superseded',data=%s::jsonb "
                    "WHERE id=%s",
                    (canonical(event), row["id"]),
                )
            data["phase"] = "waiting"
            if one_shot:
                _operation_control(data["operation"])["wait"] = None
        elif control == AssignmentControl.RESUME:
            self._assert_guidance_current(transaction, data)
            if data["lifecycle"] != "paused":
                _conflict("assignment_not_paused")
            if one_shot:
                self._validate_operation_continuation(transaction, data)
                phase = data["phase"]
                if phase in {"checking", "investigating", "delegating"}:
                    phase = "waiting"
                data.update(lifecycle="active", phase=phase)
            else:
                self._validate_references(transaction, owner_id, _definition(data["definition"]))
                data.update(lifecycle="active", phase="waiting")
        elif control == AssignmentControl.REVOKE:
            data["definition"]["offline_grant_id"] = None
            data.update(phase="waiting_authorization")
        elif control == AssignmentControl.PAUSE:
            data["lifecycle"] = "paused"
        else:
            data["lifecycle"] = "stopped"
            if one_shot and _supported(data):
                data["operation"]["terminal_outcome"] = "cancelled"
        data["control_epoch"] += 1
        data["controls"][submission_id] = {
            "signature": request,
            "submission_digest": submission_digest,
            "command": str(control),
        }
        self._clear_claim(data)
        data["next_wake_at"] = (
            plain(_now(transaction))
            if data["lifecycle"] == "active" and data["phase"] == "waiting"
            else None
        )
        if (
            one_shot
            and control == AssignmentControl.RESUME
            and data["phase"] == "failed"
            and data["next_retry_at"] is not None
        ):
            due = max(_now(transaction), _time(data["next_retry_at"]))
            operation = self._operation_spec(data["operation"], owner_id)
            if min(_time(operation.authority.expires_at), _time(operation.deadline_at)) <= due:
                _conflict("assignment_deadline_exceeded")
            data["next_wake_at"] = plain(due)
        invalidated, begun = self._invalidate_actions(
            transaction, data, conservative=control == AssignmentControl.STOP
        )
        if one_shot and _supported(data) and self._operation_continuation_held(transaction, data):
            if data["phase"] not in {
                "waiting_authorization",
                "awaiting_event",
                "waiting_approval",
                "budget_exhausted",
            }:
                data["phase"] = "reconciliation"
            data.update(next_wake_at=None, next_retry_at=None)
        for task in data["tasks"]:
            if task["state"] in {"pending", "running"}:
                task["state"] = "cancelled" if control in {"stop", "revise"} else "pending"
                task["task_generation"] += 1
        self._activity(
            transaction,
            data,
            AssignmentActivityRecord(
                f"control:{submission_id}", "control", f"Assignment {control}", "", {}
            ),
            critical=True,
        )
        return AssignmentControlResult(
            self._save(transaction, data), True, tuple(invalidated), tuple(begun)
        )

    def _validate_operation_continuation(self, transaction, data, definition=None):
        self._assert_guidance_current(transaction, data)
        _require_executable(data)
        if data["phase"] == "waiting_authorization":
            _conflict("assignment_authorization_unavailable")
        operation = self._operation_spec(data["operation"], data["owner_id"])
        self._check_operation_time(transaction, operation)
        definition = definition or _definition(data["definition"])
        if operation.kind == "research" and not definition.source:
            raise RepositoryValidationError("research requires a source plan")
        if operation.authority.origin == "scheduled" or definition.offline_grant_id:
            if (
                operation.authority.origin == "scheduled"
                and definition.offline_grant_id != operation.authority.reference_id
            ):
                _conflict("assignment_authorization_unavailable")
            self._validate_references(transaction, data["owner_id"], definition)
        else:
            self._validate_non_grant_references(transaction, data["owner_id"], definition)

    @staticmethod
    def _lock_operation_owner(transaction, owner_id):
        _text(owner_id)
        transaction.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", (owner_id,))
        retired = transaction.fetch_one(
            "SELECT state FROM astralplane_blob_owner_state WHERE owner_id=%s FOR UPDATE",
            (owner_id,),
        )
        return not retired or retired["state"] == "active"

    def _prepare_owner_event_wait(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        expected_instruction_revision,
        expected_control_epoch,
        expected_state_version,
        submission_id,
        submission_digest,
        event_key,
        source_revision,
        control_version,
    ):
        _uuid(submission_id)
        _digest(submission_digest)
        _integer(control_version, 1, 1)
        _integer(expected_instruction_revision, 1)
        _integer(expected_control_epoch, 1)
        _integer(expected_state_version, 1)
        _text(event_key, 128)
        _integer(source_revision)
        self._lock_operation_owner(transaction, owner_id)
        data = self._load(transaction, owner_id, assignment_id, lock=True)
        if data.get("execution_profile") != "one_shot":
            _conflict("assignment_profile_mismatch")
        signature = [
            "wait",
            control_version,
            event_key,
            source_revision,
            expected_instruction_revision,
            expected_control_epoch,
            expected_state_version,
            submission_digest,
        ]
        prior = data["controls"].get(submission_id)
        if prior is not None:
            if canonical(prior) != canonical(
                {
                    "signature": signature,
                    "submission_digest": submission_digest,
                    "command": "wait",
                }
            ):
                _conflict("assignment_idempotency_conflict")
            return data, signature, True, (), ()
        if data["submission_id"] == submission_id:
            _conflict("assignment_idempotency_conflict")
        _require_executable(data)
        _version(data, expected_instruction_revision, expected_control_epoch)
        _state_version(data, expected_state_version)
        if (
            data["lifecycle"] not in {"active", "paused"}
            or data["operation"].get("terminal_outcome") is not None
            or (data["phase"] == "failed" and data["next_retry_at"] is None)
        ):
            _conflict("assignment_not_waiting")
        state = data["operation"].get("control", {"watermarks": {}})
        if source_revision < state["watermarks"].get(event_key, 0):
            _conflict("assignment_event_revision_conflict")
        if len(data["controls"]) >= 256 or (
            event_key not in state["watermarks"] and len(state["watermarks"]) >= 64
        ):
            _conflict("assignment_history_capacity_exhausted")
        actions, unresolved, _ = self._purge_blockers(transaction, data)
        invalidated = tuple(
            action["action_id"]
            for action in actions
            if action["state"] in {"ready", "proposed", "approved", "reserved"}
            and not any(a["state"] in {"started", "uncertain"} for a in action["attempts"])
        )
        begun = tuple(identity for identity in unresolved if identity not in invalidated)
        return data, signature, False, invalidated, begun

    def prepare_owner_event_wait(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        expected_instruction_revision,
        expected_control_epoch,
        expected_state_version,
        submission_id,
        submission_digest,
        event_key,
        source_revision,
        control_version=1,
    ):
        data, _, replayed, invalidated, begun = self._prepare_owner_event_wait(
            transaction,
            owner_id=owner_id,
            assignment_id=assignment_id,
            expected_instruction_revision=expected_instruction_revision,
            expected_control_epoch=expected_control_epoch,
            expected_state_version=expected_state_version,
            submission_id=submission_id,
            submission_digest=submission_digest,
            event_key=event_key,
            source_revision=source_revision,
            control_version=control_version,
        )
        return AssignmentOwnerWaitPreparation(_record(data), replayed, invalidated, begun)

    def set_owner_event_wait(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        expected_instruction_revision,
        expected_control_epoch,
        expected_state_version,
        submission_id,
        submission_digest,
        event_key,
        source_revision,
        control_version=1,
    ):
        with transaction.savepoint("assignment_owner_wait_" + uuid.uuid4().hex):
            data, signature, replayed, _, _ = self._prepare_owner_event_wait(
                transaction,
                owner_id=owner_id,
                assignment_id=assignment_id,
                expected_instruction_revision=expected_instruction_revision,
                expected_control_epoch=expected_control_epoch,
                expected_state_version=expected_state_version,
                submission_id=submission_id,
                submission_digest=submission_digest,
                event_key=event_key,
                source_revision=source_revision,
                control_version=control_version,
            )
            if replayed:
                return AssignmentControlResult(_record(data), False)
            data["controls"][submission_id] = {
                "signature": signature,
                "submission_digest": submission_digest,
                "command": "wait",
            }
            state = _operation_control(data["operation"])
            state["wait"] = {"event_key": event_key, "source_revision": source_revision}
            state["watermarks"].setdefault(event_key, 0)
            data["control_epoch"] += 1
            self._clear_claim(data)
            invalidated, begun = self._invalidate_actions(transaction, data, conservative=True)
            for task in data["tasks"]:
                if task["state"] in {"pending", "running"}:
                    task["state"] = "pending"
                    task["task_generation"] += 1
            _, _, held = self._purge_blockers(transaction, data)
            held = held or data["phase"] == "reconciliation"
            data.update(
                phase="reconciliation" if held else "awaiting_event",
                next_wake_at=None,
                next_retry_at=None,
                wake_reason="event_wait",
            )
            if not held:
                data["safe_error_code"] = None
            self._activity(
                transaction,
                data,
                AssignmentActivityRecord(
                    f"control:{submission_id}", "control", "Assignment wait", ""
                ),
                critical=True,
            )
            return AssignmentControlResult(
                self._save(transaction, data), True, tuple(invalidated), tuple(begun)
            )

    def set_event_wait(
        self,
        transaction,
        *,
        fence,
        expected_state_version,
        checkpoint,
        completion_digest,
        event_key,
        source_revision,
        control_version=1,
    ):
        _integer(control_version, 1, 1)
        _text(event_key, 128)
        _integer(source_revision)
        return self.finish_episode(
            transaction,
            fence=fence,
            completion=AssignmentEpisodeCompletion(
                expected_state_version=expected_state_version,
                checkpoint=checkpoint,
                completion_digest=completion_digest,
                phase="awaiting_event",
                wake_reason="event_wait",
                event_wait={"event_key": event_key, "source_revision": source_revision},
            ),
        )

    def _prepare_wake(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        expected_state_version,
        expected_instruction_revision,
        expected_control_epoch,
        event_id,
        event_key,
        source_revision,
        event_digest,
        control_version,
    ):
        _integer(control_version, 1, 1)
        _integer(expected_state_version, 1)
        _integer(expected_instruction_revision, 1)
        _integer(expected_control_epoch, 1)
        _text(event_id, 128)
        _text(event_key, 128)
        _integer(source_revision)
        _digest(event_digest)
        owner_active = self._lock_operation_owner(transaction, owner_id)
        data = self._load(transaction, owner_id, assignment_id, lock=True)
        if data.get("execution_profile") != "one_shot":
            _conflict("assignment_operation_required")
        state = _operation_control(data["operation"])
        signature = digest([event_key, source_revision, event_digest])
        prior = state["wake_receipts"].get(event_id)
        if prior is not None:
            if prior != signature:
                _conflict("assignment_idempotency_conflict")
            return data, signature, True
        _version(data, expected_instruction_revision, expected_control_epoch)
        _state_version(data, expected_state_version)
        if data["lifecycle"] != "active" or data["phase"] != "awaiting_event":
            _conflict("assignment_not_waiting")
        if not owner_active:
            _conflict("assignment_owner_retired")
        self._validate_operation_continuation(transaction, data)
        wait = state["wait"]
        if wait is None or wait["event_key"] != event_key:
            _conflict("assignment_event_key_conflict")
        if source_revision <= max(wait["source_revision"], state["watermarks"].get(event_key, 0)):
            _conflict("assignment_event_revision_conflict")
        if len(state["wake_receipts"]) >= 128:
            _conflict("assignment_history_capacity_exhausted")
        return data, signature, False

    def prepare_wake(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        expected_state_version,
        expected_instruction_revision,
        expected_control_epoch,
        event_id,
        event_key,
        source_revision,
        event_digest,
        control_version=1,
    ):
        data, _, replayed = self._prepare_wake(
            transaction,
            owner_id=owner_id,
            assignment_id=assignment_id,
            expected_state_version=expected_state_version,
            expected_instruction_revision=expected_instruction_revision,
            expected_control_epoch=expected_control_epoch,
            event_id=event_id,
            event_key=event_key,
            source_revision=source_revision,
            event_digest=event_digest,
            control_version=control_version,
        )
        return AssignmentWakePreparation(_record(data), replayed)

    def assert_operation_continuation_clear(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        expected_instruction_revision,
        expected_control_epoch,
        expected_state_version,
    ):
        _integer(expected_instruction_revision, 1)
        _integer(expected_control_epoch, 1)
        _integer(expected_state_version, 1)
        owner_active = self._lock_operation_owner(transaction, owner_id)
        data = self._load(transaction, owner_id, assignment_id, lock=True)
        if data.get("execution_profile") != "one_shot":
            _conflict("assignment_operation_required")
        _require_executable(data)
        _version(data, expected_instruction_revision, expected_control_epoch)
        _state_version(data, expected_state_version)
        if not owner_active:
            _conflict("assignment_owner_retired")
        operation = self._operation_spec(data["operation"], owner_id)
        self._check_operation_time(transaction, operation)
        if self._operation_continuation_held(transaction, data):
            _conflict("assignment_action_uncertain")
        self._check_operation_time(transaction, operation)
        return _record(data)

    def accept_wake(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        expected_state_version,
        expected_instruction_revision,
        expected_control_epoch,
        event_id,
        event_key,
        source_revision,
        event_digest,
        control_version=1,
    ):
        data, signature, replayed = self._prepare_wake(
            transaction,
            owner_id=owner_id,
            assignment_id=assignment_id,
            expected_state_version=expected_state_version,
            expected_instruction_revision=expected_instruction_revision,
            expected_control_epoch=expected_control_epoch,
            event_id=event_id,
            event_key=event_key,
            source_revision=source_revision,
            event_digest=event_digest,
            control_version=control_version,
        )
        if replayed:
            return AssignmentControlResult(_record(data), False)
        state = _operation_control(data["operation"])
        state["wake_receipts"][event_id] = signature
        state["watermarks"][event_key] = source_revision
        state["wait"] = None
        data.update(
            phase="waiting",
            wake_reason="event",
            next_wake_at=plain(_now(transaction)),
            wake_generation=data["wake_generation"] + 1,
        )
        return AssignmentControlResult(self._save(transaction, data), True)

    def request_check(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        expected_instruction_revision,
        expected_control_epoch,
        submission_id,
        submission_digest,
    ):
        _uuid(submission_id)
        _digest(submission_digest)
        data = self._load(transaction, owner_id, assignment_id, lock=True)
        _version(data, expected_instruction_revision, expected_control_epoch)
        if data["lifecycle"] != "active":
            _conflict("assignment_not_active")
        if data.get("execution_profile") == "one_shot":
            _conflict("assignment_operation_requires_explicit_wake")
        key = "check:" + submission_id
        if key in data["controls"]:
            if data["controls"][key] != submission_digest:
                _conflict("assignment_idempotency_conflict")
            return _record(data)
        if len(data["controls"]) >= 256:
            _conflict("assignment_history_capacity_exhausted")
        data["controls"][key] = submission_digest
        now = _now(transaction)
        due = now
        if data["last_check_at"]:
            due = max(
                due,
                _time(data["last_check_at"])
                + timedelta(seconds=data["definition"]["limits"]["cadence_seconds"]),
            )
        if data["next_retry_at"]:
            due = max(due, _time(data["next_retry_at"]))
        data.update(
            next_wake_at=plain(due),
            wake_reason="owner_check",
            wake_generation=data["wake_generation"] + 1,
        )
        return self._save(transaction, data)

    def claim_due_for_administration(self, transaction, *, worker_id, limit=20, lease_seconds=30):
        return self._claim_due(transaction, worker_id, limit, lease_seconds, "persistent")

    def claim_operations_for_administration(
        self, transaction, *, worker_id, limit=20, lease_seconds=30
    ):
        _conflict("assignment_authorization_unavailable")

    def discover_due_operations_for_administration(
        self, query, *, limit=20, after_due_at=None, after_id=None
    ):
        _integer(limit, 1, 100)
        if (after_due_at is None) != (after_id is None):
            raise RepositoryValidationError("complete operation discovery cursor required")
        if after_id is not None:
            _uuid(after_id)
            after_due_at = _time(after_due_at)
        rows = query.fetch_all(
            "SELECT * FROM persistent_assignment WHERE execution_profile='one_shot' "
            "AND lifecycle='active' AND next_wake_at<=clock_timestamp() "
            "AND lease_expires_at IS NULL AND data->>'phase' IN ('waiting','failed') "
            "AND data->'operation'->'version'='2'::jsonb "
            "AND COALESCE(data->'operation'->'control'->'version','1'::jsonb)='1'::jsonb "
            "AND COALESCE(data->'checkpoint'->'schema_version','1'::jsonb)='1'::jsonb "
            "AND ((data->'operation'->'authority'->>'origin'='interactive' "
            "AND data->'operation'->'authority'->>'reference_kind'='session_incarnation') "
            "OR (data->'operation'->'authority'->>'origin'='framework' "
            "AND data->'operation'->'authority'->>'reference_kind'='credential' "
            "AND data->'operation'->>'kind'='chat' "
            "AND EXISTS (SELECT 1 FROM framework_credential fc "
            "WHERE fc.owner_id=persistent_assignment.owner_user_id "
            "AND fc.id::text=data->'operation'->'authority'->>'reference_id' "
            "AND fc.revoked_at IS NULL AND fc.expires_at>clock_timestamp() "
            "AND fc.scopes ? 'operations.submit'))) "
            "AND (%s::timestamptz IS NULL OR (next_wake_at,id)>(%s,%s::uuid)) "
            "ORDER BY next_wake_at,id LIMIT %s",
            (after_due_at, after_due_at, after_id, limit),
        )
        return tuple(
            _record(self._validated_assignment_row(row, row["owner_user_id"], str(row["id"])))
            for row in rows
        )

    def capture_framework_operation_execution_observation(
        self, transaction, *, owner_id, assignment_id, expected_record
    ):
        _uuid(assignment_id)
        if (
            type(expected_record) is not AssignmentRecord
            or expected_record.owner_id != owner_id
            or expected_record.assignment_id != assignment_id
        ):
            _conflict("assignment_authorization_unavailable")
        if not self._lock_operation_owner(transaction, owner_id):
            _conflict("assignment_owner_retired")
        selected = self._load(transaction, owner_id, assignment_id)
        _require_executable(selected)
        operation = selected.get("operation", {})
        authority = operation.get("authority", {})
        definition = selected["definition"]
        if (
            selected.get("execution_profile") != "one_shot"
            or selected.get("lifecycle") != "active"
            or operation.get("version") != 2
            or operation.get("kind") != "chat"
            or authority.get("origin") != "framework"
            or authority.get("reference_kind") != "credential"
            or authority.get("owner_id") != owner_id
            or definition.get("source")
            or definition.get("allowed_tools")
            or definition.get("consented_scopes")
            or definition.get("offline_grant_id")
            or operation.get("selection") is not None
        ):
            _conflict("assignment_authorization_unavailable")
        _uuid(authority["reference_id"])
        observation = FrameworkCredentialRepository()._capture_operation_execution_observation(
            transaction,
            owner_id=owner_id,
            credential_id=authority["reference_id"],
            expires_at=min(_time(authority["expires_at"]), _time(operation["deadline_at"])),
        )
        current = self._load(transaction, owner_id, assignment_id, lock=True)
        if _record(current) != expected_record:
            _conflict("assignment_state_changed")
        self._validate_operation_continuation(transaction, current)
        if (
            self.get_selected_input(transaction, owner_id=owner_id, assignment_id=assignment_id)
            is not None
        ):
            _conflict("assignment_authorization_unavailable")
        self._assert_guidance_current(transaction, current, observation.valid_until)
        if not self._lock_execution_authority(transaction, current, observation):
            _conflict("assignment_authorization_unavailable")
        return observation

    def claim_operation_for_administration(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        expected_state_version,
        worker_id,
        authority,
        lease_seconds=30,
    ):
        _integer(expected_state_version, 1)
        with transaction.savepoint("operation_claim_" + uuid.uuid4().hex):
            data = self._operation_claim_context(transaction, owner_id, assignment_id, authority)
            _state_version(data, expected_state_version)
            if (
                data["lifecycle"] != "active"
                or data["phase"] not in {"waiting", "failed"}
                or data["lease_expires_at"] is not None
                or _time(data["next_wake_at"]) is None
                or _time(data["next_wake_at"]) > _now(transaction)
            ):
                _conflict("assignment_not_due")
            if (
                self._operation_continuation_held(transaction, data)
                or data["operation"].get("control", {}).get("wait") is not None
            ):
                _conflict("assignment_action_uncertain")
            self._assert_operation_claim_current(transaction, data, authority)
            claim = self._claim(transaction, data, worker_id, lease_seconds)
            self._assert_operation_claim_current(transaction, data, authority)
            return claim

    def _operation_claim_context(self, transaction, owner_id, assignment_id, authority):
        if not self._lock_operation_owner(transaction, owner_id):
            _conflict("assignment_owner_retired")
        selected = self._load(transaction, owner_id, assignment_id)
        if selected.get("execution_profile") != "one_shot":
            _conflict("assignment_operation_required")
        _require_executable(selected)
        if not self._lock_execution_authority(transaction, selected, authority):
            _conflict("assignment_authorization_unavailable")
        data = self._load(transaction, owner_id, assignment_id, lock=True)
        if self._execution_authority_selection(data) != self._execution_authority_selection(
            selected
        ):
            _conflict("assignment_authorization_unavailable")
        self._assert_operation_claim_current(transaction, data, authority)
        return data

    def _assert_operation_claim_current(self, transaction, data, authority):
        self._validate_operation_continuation(transaction, data)
        if not self._lock_execution_authority(transaction, data, authority):
            _conflict("assignment_authorization_unavailable")

    def _claim_due(self, transaction, worker_id, limit, lease_seconds, profile):
        _integer(limit, 1, 100)
        rows = transaction.fetch_all(
            "SELECT id,owner_user_id FROM persistent_assignment WHERE lifecycle='active' "
            "AND next_wake_at<=clock_timestamp() AND lease_expires_at IS NULL "
            "AND data->>'phase' IN ('waiting','failed') AND execution_profile=%s "
            "ORDER BY next_wake_at,id LIMIT %s FOR UPDATE SKIP LOCKED",
            (profile, limit),
        )
        return tuple(
            self._claim(
                transaction,
                self._load(transaction, row["owner_user_id"], str(row["id"]), lock=True),
                worker_id,
                lease_seconds,
            )
            for row in rows
        )

    def bind_operation(self, transaction, *, fence, binding):
        data = self._fenced(
            transaction, fence, action_id=self._foreground_action(transaction, fence)
        )
        _uuid(binding.operation_id)
        _uuid(binding.execution_lease_token)
        _integer(binding.execution_generation, 1)
        if data["operation_binding"] not in (None, plain(binding)):
            _conflict("assignment_operation_conflict")
        data["operation_binding"] = plain(binding)
        return self._save(transaction, data)

    def _foreground_action(self, transaction, fence):
        return self._load(transaction, fence.owner_id, fence.assignment_id).get(
            "approved_action_id"
        )

    def renew_claim(self, transaction, *, fence, lease_seconds=30):
        _integer(lease_seconds, 5, 60)
        data = self._fenced(
            transaction, fence, action_id=self._foreground_action(transaction, fence)
        )
        data["lease_expires_at"] = plain(_now(transaction) + timedelta(seconds=lease_seconds))
        return self._claim_record(data, self._save(transaction, data))

    def assert_current_claim(self, query, *, fence):
        return _record(self._fenced(query, fence, action_id=self._foreground_action(query, fence)))

    @staticmethod
    def _execution_authority_selection(data):
        return (
            data["operation"]["authority"] if data.get("execution_profile") == "one_shot" else None,
            data["definition"]["offline_grant_id"],
        )

    def _lock_execution_authority(self, transaction, data, authority):
        selected, grant_id = self._execution_authority_selection(data)
        if selected is not None:
            if not _executable(data):
                return False
            origin = selected["origin"]
            if origin == "interactive":
                if not isinstance(authority, SessionExecutionObservation):
                    return False
                try:
                    state = authority.credential
                    if (
                        state.owner_id != data["owner_id"]
                        or state.incarnation_id != selected["reference_id"]
                    ):
                        return False
                    SessionRepository().assert_current_execution(transaction, observation=authority)
                except (
                    AttributeError,
                    RepositoryConflictError,
                    RepositoryDataError,
                    RepositoryNotFoundError,
                    RepositoryValidationError,
                ):
                    return False
            elif origin == "framework":
                if not isinstance(authority, FrameworkCredentialObservation):
                    return False
                try:
                    state = authority.credential
                    if (
                        state.owner_id != data["owner_id"]
                        or state.credential_id != selected["reference_id"]
                    ):
                        return False
                    current = FrameworkCredentialRepository().assert_current_execution(
                        transaction, observation=authority
                    )
                    if (
                        "operations.submit" not in state.scopes
                        or "operations.submit" not in current.credential.scopes
                    ):
                        return False
                except (
                    AttributeError,
                    RepositoryConflictError,
                    RepositoryDataError,
                    RepositoryNotFoundError,
                    RepositoryValidationError,
                ):
                    return False
            else:
                return False
        return (
            grant_id is None
            or transaction.fetch_one(
                "SELECT id FROM user_offline_grant WHERE id=%s AND user_id=%s FOR UPDATE",
                (grant_id, data["owner_id"]),
            )
            is not None
        )

    def assert_current_assignment_execution(
        self,
        transaction,
        *,
        fence,
        binding,
        action_id=None,
        authority=None,
    ):
        if not isinstance(fence, AssignmentFence) or not isinstance(
            binding, AssignmentOperationBinding
        ):
            raise RepositoryValidationError("typed execution fences required")
        _text(fence.owner_id)
        _uuid(fence.assignment_id)
        _uuid(fence.claim_token)
        _integer(fence.instruction_revision, 1)
        _integer(fence.control_epoch, 1)
        _integer(fence.claim_generation, 1)
        if action_id is not None:
            _uuid(action_id)
        if not self._lock_operation_owner(transaction, fence.owner_id):
            _conflict("assignment_owner_retired")
        selected = self._load(transaction, fence.owner_id, fence.assignment_id)
        if not self._lock_execution_authority(transaction, selected, authority):
            _conflict("assignment_authorization_unavailable")
        data = self._fenced(transaction, fence, action_id=action_id)
        if self._execution_authority_selection(data) != self._execution_authority_selection(
            selected
        ):
            _conflict("assignment_authorization_unavailable")
        self._assert_guidance_current(transaction, data)
        self._assert_bound_admission(transaction, data, binding)
        data = self._fenced(transaction, fence, action_id=action_id)
        if not self._lock_execution_authority(transaction, data, authority):
            _conflict("assignment_authorization_unavailable")
        if data.get("execution_profile") == "one_shot":
            self._validate_operation_continuation(transaction, data)
        else:
            self._validate_references(transaction, fence.owner_id, _definition(data["definition"]))
        self._assert_guidance_current(transaction, data)
        return _record(data)

    @staticmethod
    def _assert_bound_admission(transaction, data, binding):
        if data["operation_binding"] != plain(binding):
            _conflict("assignment_operation_conflict")
        _uuid(binding.operation_id)
        _uuid(binding.execution_lease_token)
        _integer(binding.execution_generation, 1)
        operation = WorkAdmissionRepository().assert_current_execution(
            transaction,
            ExecutionFence(
                uuid.UUID(binding.operation_id),
                binding.execution_generation,
                uuid.UUID(binding.execution_lease_token),
            ),
        )
        if operation.owner_user_id != data["owner_id"]:
            _conflict("assignment_operation_conflict")

    def put_action_for_execution(self, transaction, *, fence, binding, intent, authority=None):
        with transaction.savepoint("assignment_prepare_" + uuid.uuid4().hex):
            self.assert_current_assignment_execution(
                transaction, fence=fence, binding=binding, authority=authority
            )
            self._assert_operation_action_continuable(transaction, fence, binding)
            result = self.put_action(transaction, fence=fence, intent=intent)
            self.assert_current_assignment_execution(
                transaction, fence=fence, binding=binding, authority=authority
            )
        return result

    def reserve_action_for_execution(
        self,
        transaction,
        *,
        fence,
        binding,
        action_id,
        attempt_id,
        expected_request_digest,
        maximum,
        quote_digest=None,
        quote_expires_at=None,
        authority=None,
    ):
        with transaction.savepoint("assignment_reserve_" + uuid.uuid4().hex):
            self.assert_current_assignment_execution(
                transaction, fence=fence, binding=binding, action_id=action_id, authority=authority
            )
            self._assert_operation_action_continuable(transaction, fence, binding)
            result = self.reserve_action(
                transaction,
                fence=fence,
                action_id=action_id,
                attempt_id=attempt_id,
                expected_request_digest=expected_request_digest,
                maximum=maximum,
                quote_digest=quote_digest,
                quote_expires_at=quote_expires_at,
            )
            self.assert_current_assignment_execution(
                transaction, fence=fence, binding=binding, action_id=action_id, authority=authority
            )
        return result

    def start_action_for_execution(
        self,
        transaction,
        *,
        fence,
        action_id,
        attempt_id,
        expected_request_digest,
        current_permission_digest,
        current_precondition_digest,
        binding,
        interactive_receipt_id=None,
        authority=None,
    ):
        with transaction.savepoint("assignment_permit_" + uuid.uuid4().hex):
            self.assert_current_assignment_execution(
                transaction, fence=fence, binding=binding, action_id=action_id, authority=authority
            )
            self._assert_operation_action_continuable(transaction, fence, binding)
            result = self.start_action(
                transaction,
                fence=fence,
                action_id=action_id,
                attempt_id=attempt_id,
                expected_request_digest=expected_request_digest,
                current_permission_digest=current_permission_digest,
                current_precondition_digest=current_precondition_digest,
                binding=binding,
                interactive_receipt_id=interactive_receipt_id,
            )
            self.assert_current_assignment_execution(
                transaction, fence=fence, binding=binding, action_id=action_id, authority=authority
            )
        return result

    def _action(
        self, transaction, owner_id, assignment_id, action_id, *, required=True, inspect_only=False
    ):
        _uuid(action_id)
        row = transaction.fetch_one(
            "SELECT data,state FROM persistent_assignment_action "
            "WHERE id=%s AND assignment_id=%s AND owner_user_id=%s FOR UPDATE",
            (action_id, assignment_id, owner_id),
        )
        if row is None:
            if required:
                raise RepositoryNotFoundError("assignment action not found")
            return None
        try:
            data = plain(row["data"])
            if (
                data["action_id"] != action_id
                or data["assignment_id"] != assignment_id
                or data["owner_id"] != owner_id
                or data["state"] != row["state"]
            ):
                raise ValueError
            _action_record(data)
            if not inspect_only:
                self._validate_action_payloads(data)
            return data
        except (KeyError, TypeError, ValueError) as exc:
            raise RepositoryDataError("invalid persisted action") from exc

    @staticmethod
    def _validate_action_payloads(action):
        intent = action["intent"]
        transient = intent.get("transient_input")
        if transient is not None:
            _transient_input(transient, intent["request"], intent["request_digest"])
        for attempt in action["attempts"]:
            for key in ("outcome", "uncertain_observation"):
                outcome = attempt.get(key)
                if outcome is not None:
                    disposition = None
                    if outcome.get("result_disposition") is not None:
                        disposition = _result_disposition(
                            outcome["result_disposition"], outcome["result"]
                        )
                    if transient is not None:
                        _transient_receipt(disposition, outcome.get("evidence_reference"))

    @staticmethod
    def _save_action(transaction, data):
        transaction.execute(
            "UPDATE persistent_assignment_action SET data=%s::jsonb,state=%s "
            "WHERE id=%s AND assignment_id=%s AND owner_user_id=%s",
            (
                canonical(data),
                data["state"],
                data["action_id"],
                data["assignment_id"],
                data["owner_id"],
            ),
        )
        return _action_record(data)

    def _known_action(self, transaction, owner_id, assignment_id, action_id):
        try:
            action = self._action(transaction, owner_id, assignment_id, action_id)
            if action["intent"]["request"].get("kind") == "result_publication":
                from astralplane.repositories.result_publications import known_publication_action

                return known_publication_action(action)
            required = {
                "action_id",
                "assignment_id",
                "owner_id",
                "intent",
                "intent_digest",
                "instruction_revision",
                "control_epoch",
                "state",
                "result",
                "attempts",
                "decision",
                "foreground_admission",
                "reconciliation",
            }
            if not required <= action.keys() or action.keys() - required - {
                "interactive_proposal_id",
                "approval_consumed_at",
            }:
                return None
            if action["intent_digest"] != digest(action["intent"]):
                return None
            _integer(action["instruction_revision"], 1)
            _integer(action["control_epoch"], 1)
            self._amount(AssignmentResourceAmount(**action["intent"]["maximum"]))
            for key, model in (
                ("decision", AssignmentActionDecision),
                ("reconciliation", AssignmentActionReconciliation),
            ):
                if action[key] is not None:
                    decision = model(**action[key])
                    _uuid(decision.submission_id)
                    _digest(decision.submission_digest)
                    if key == "decision":
                        if decision.decision not in {"approve", "decline"}:
                            return None
                        for value in (
                            decision.proposal_digest,
                            decision.permission_digest,
                            decision.precondition_digest,
                        ):
                            _digest(value)
                    else:
                        if decision.decision not in {"confirmed_applied", "confirmed_not_applied"}:
                            return None
                        _digest(decision.prior_result_digest)
                        _text(decision.evidence_reference, 2048)
            if action["result"] is not None and (
                not isinstance(action["result"], dict)
                or action["result"].keys()
                - {
                    "outcome",
                    "result_digest",
                    "result",
                    "evidence_reference",
                    "actual",
                    "result_available",
                    "reconciliation",
                    "result_disposition",
                    "reacquisition_reason",
                }
            ):
                return None
            if action.get("interactive_proposal_id") is not None:
                _text(action["interactive_proposal_id"], 128)
            _time(action.get("approval_consumed_at"))
            if action["foreground_admission"] is not None and set(
                action["foreground_admission"]
            ) != {"submission_id", "submission_digest", "receipt_id", "claim_generation"}:
                return None
            for attempt in action["attempts"]:
                required_attempt = {
                    "attempt_id",
                    "state",
                    "maximum",
                    "quote_digest",
                    "quote_expires_at",
                    "dispatch_token",
                    "binding",
                    "outcome",
                }
                if not required_attempt <= attempt.keys() or (
                    attempt.keys()
                    - required_attempt
                    - {"uncertain_observation", "assignment_fence", "settlement_signature"}
                ):
                    return None
                _uuid(attempt["attempt_id"])
                if "assignment_fence" in attempt:
                    issued = AssignmentFence(**attempt["assignment_fence"])
                    if issued.owner_id != owner_id or issued.assignment_id != assignment_id:
                        return None
                    _version(action, issued.instruction_revision, issued.control_epoch)
                    _integer(issued.claim_generation, 1)
                    _uuid(issued.claim_token)
                if "settlement_signature" in attempt:
                    _digest(attempt["settlement_signature"])
                if attempt["state"] not in {
                    "reserved",
                    "started",
                    "uncertain",
                    "succeeded",
                    "failed",
                    "failed_not_started",
                }:
                    return None
                self._amount(AssignmentResourceAmount(**attempt["maximum"]))
                _time(attempt["quote_expires_at"])
                if attempt["binding"] is not None:
                    AssignmentOperationBinding(**attempt["binding"])
                if attempt["dispatch_token"] is not None:
                    _uuid(attempt["dispatch_token"])
                    if attempt["binding"] is None:
                        return None
                    if (
                        attempt["state"] not in {"started", "uncertain"}
                        and attempt["outcome"] is None
                        and action["reconciliation"] is None
                    ):
                        return None
                for key in ("outcome", "uncertain_observation"):
                    if attempt.get(key) is not None:
                        result = AssignmentActionOutcome(**attempt[key])
                        if result.actual is not None:
                            self._amount(AssignmentResourceAmount(**result.actual))
                        if result.outcome not in {
                            "succeeded",
                            "failed",
                            "failed_not_started",
                            "uncertain",
                        }:
                            return None
                observed = attempt["outcome"]
                if attempt["dispatch_token"] is None and (
                    observed is not None or attempt["state"] in {"succeeded", "failed"}
                ):
                    return None
                if observed is not None and observed["outcome"] != attempt["state"]:
                    if observed["outcome"] != "uncertain" or not self._reconciled_attempt(
                        action, attempt, observed["result_digest"]
                    ):
                        return None
                elif (
                    observed is None
                    and attempt["dispatch_token"] is not None
                    and attempt["state"] not in {"started", "uncertain"}
                    and not self._reconciled_attempt(
                        action, attempt, digest([action_id, "lease_expired"])
                    )
                ):
                    return None
            attempts = action["attempts"]
            if action["state"] in {"succeeded", "failed"} or (
                action["state"] == "failed_not_started"
                and attempts
                and attempts[-1]["dispatch_token"] is not None
            ):
                if not attempts:
                    return None
                final = attempts[-1]
                if action["state"] != final["state"] or action["result"] != (
                    _outcome_projection(final["outcome"]) if final["outcome"] is not None else None
                ):
                    prior = (final["outcome"] or {}).get(
                        "result_digest", digest([action_id, "lease_expired"])
                    )
                    if not self._reconciled_attempt(action, final, prior):
                        return None
            return action
        except (
            RepositoryDataError,
            RepositoryConflictError,
            RepositoryValidationError,
            KeyError,
            TypeError,
            ValueError,
            AttributeError,
        ):
            return None

    @staticmethod
    def _reconciled_attempt(action, attempt, prior_digest):
        decision = action["reconciliation"]
        if decision is None or attempt is not action["attempts"][-1]:
            return False
        expected = (
            "succeeded" if decision["decision"] == "confirmed_applied" else "failed_not_started"
        )
        return (
            decision["prior_result_digest"] == prior_digest
            and action["state"] == attempt["state"] == expected
            and action["result"]
            == {
                "outcome": "reconciled_applied"
                if expected == "succeeded"
                else "reconciled_not_applied",
                "result_digest": digest(["reconciliation", decision]),
                "result": {},
                "result_available": False,
                "evidence_reference": decision["evidence_reference"],
                "reconciliation": {
                    "decision": decision["decision"],
                    "prior_result_digest": prior_digest,
                },
            }
        )

    def _invalidate_actions(self, transaction, assignment, *, conservative=False):
        rows = transaction.fetch_all(
            "SELECT id FROM persistent_assignment_action WHERE assignment_id=%s "
            "AND owner_user_id=%s AND (%s OR state IN "
            "('ready','proposed','approved','reserved','started','uncertain')) "
            "ORDER BY id FOR UPDATE",
            (assignment["assignment_id"], assignment["owner_id"], conservative),
        )
        invalidated, begun = [], []
        for row in rows:
            action = (self._known_action if conservative else self._action)(
                transaction, assignment["owner_id"], assignment["assignment_id"], str(row["id"])
            )
            if (
                action is None
                or action["state"] in {"started", "uncertain"}
                or any(
                    attempt["state"] in {"started", "uncertain"} for attempt in action["attempts"]
                )
            ):
                begun.append(str(row["id"]))
                continue
            if action["state"] not in {"ready", "proposed", "approved", "reserved"}:
                continue
            for attempt in action["attempts"]:
                if attempt["state"] == "reserved":
                    self._release(assignment, attempt["maximum"])
                    attempt["state"] = "failed_not_started"
            action["state"] = "invalidated"
            if action.get("interactive_proposal_id"):
                self._expire_interactive_proposal(
                    transaction, assignment["owner_id"], action["interactive_proposal_id"]
                )
            self._save_action(transaction, action)
            invalidated.append(action["action_id"])
        return invalidated, begun

    @staticmethod
    def _expire_interactive_proposal(transaction, owner_id, proposal_id):
        transaction.execute(
            "UPDATE remote_operation_proposal SET status='expired',decided_at=COALESCE(decided_at, "
            "GREATEST(created_at,floor(extract(epoch FROM clock_timestamp()))::bigint)) "
            "WHERE owner_user_id=%s AND proposal_id=%s AND status IN ('pending','approved')",
            (owner_id, proposal_id),
        )

    @staticmethod
    def _amount(amount):
        values = plain(amount)
        for key in _DIMENSIONS:
            _integer(values[key])
        money = values.get("spend_micro_units")
        if money is not None:
            _integer(money)
            _text(values.get("currency"), 8)
        elif values.get("currency") is not None:
            raise RepositoryValidationError("unknown money cannot carry a currency")
        basis = values.get("basis")
        if basis is not None:
            _validate_usage_basis(basis)
        return values

    @staticmethod
    def _day(data, now):
        day = plain(now)[:10]
        if data["usage"]["day"] != day:
            data["usage"]["day"] = day
            data["usage"]["daily"] = {}

    @staticmethod
    def _release(data, maximum):
        outstanding = data["usage"]["outstanding"]
        for key in (*_DIMENSIONS, "spend_micro_units"):
            amount = maximum.get(key)
            if amount is not None:
                if outstanding.get(key, 0) < amount:
                    raise RepositoryDataError("reservation accounting is inconsistent")
                outstanding[key] = outstanding.get(key, 0) - amount

    def _reserve(self, data, amount):
        usage, limits = data["usage"], data["definition"]["limits"]
        keys = list(_DIMENSIONS)
        if limits.get("spend_micro_units") is not None:
            if amount.get("currency") != limits["currency"]:
                _conflict("assignment_cost_bound_unavailable")
            keys.append("spend_micro_units")
        for key in keys:
            value = amount.get(key)
            if value is None:
                _conflict("assignment_cost_bound_unavailable")
            outstanding = usage["outstanding"].get(key, 0)
            if usage["spent"].get(key, 0) + outstanding + value > limits[key] or usage["daily"].get(
                key, 0
            ) + outstanding + value > limits.get("daily_" + key, limits[key]):
                _conflict("assignment_budget_exhausted")
        for key in (*_DIMENSIONS, "spend_micro_units"):
            if amount.get(key) is not None:
                usage["outstanding"][key] = usage["outstanding"].get(key, 0) + amount[key]

    def read_result_publication_destination(
        self,
        transaction,
        *,
        owner_id,
        conversation_id,
        expected_render_revision,
        expected_publication_id,
        maximum_bytes=1048576,
    ):
        from astralplane.repositories.result_publication_destination import read

        return read(
            self,
            transaction,
            owner_id=owner_id,
            conversation_id=conversation_id,
            expected_render_revision=expected_render_revision,
            expected_publication_id=expected_publication_id,
            maximum_bytes=maximum_bytes,
        )

    def put_result_publication_proposal(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        expected_instruction_revision,
        expected_control_epoch,
        expected_state_version,
        proposal: ResultPublicationProposal,
        content: ResultPublicationContent,
        expected_selected: AssignmentSelectedInput | None,
        authority: SessionExecutionObservation,
        caller_valid_until: datetime,
    ) -> AssignmentActionRecord:
        from astralplane.repositories.result_publications import put

        return put(
            self,
            transaction,
            owner_id=owner_id,
            assignment_id=assignment_id,
            expected_instruction_revision=expected_instruction_revision,
            expected_control_epoch=expected_control_epoch,
            expected_state_version=expected_state_version,
            proposal=proposal,
            content=content,
            expected_selected=expected_selected,
            authority=authority,
            caller_valid_until=caller_valid_until,
        )

    def prepare_result_publication(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        action_id,
        decision: AssignmentActionDecision,
        expected_state_version,
        authority: SessionExecutionObservation | None = None,
        caller_valid_until: datetime | None = None,
    ) -> ResultPublicationPreparation:
        from astralplane.repositories.result_publications import prepare

        return prepare(
            self,
            transaction,
            owner_id=owner_id,
            assignment_id=assignment_id,
            action_id=action_id,
            decision=decision,
            expected_state_version=expected_state_version,
            authority=authority,
            caller_valid_until=caller_valid_until,
        )

    def commit_result_publication(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        action_id,
        decision: AssignmentActionDecision,
        expected_state_version,
        content: ResultPublicationContent,
        authority: SessionExecutionObservation | None = None,
        caller_valid_until: datetime | None = None,
    ) -> ResultPublicationReceipt:
        from astralplane.repositories.result_publications import commit

        return commit(
            self,
            transaction,
            owner_id=owner_id,
            assignment_id=assignment_id,
            action_id=action_id,
            decision=decision,
            expected_state_version=expected_state_version,
            content=content,
            authority=authority,
            caller_valid_until=caller_valid_until,
        )

    def put_action(self, transaction, *, fence, intent):
        if (
            isinstance(intent.request, Mapping)
            and intent.request.get("kind") == "result_publication"
        ):
            raise RepositoryValidationError("owner publication cannot use a worker permit")
        data = self._fenced(transaction, fence)
        _text(intent.action_key)
        _digest(intent.request_digest)
        _digest(intent.permission_digest)
        _digest(intent.precondition_digest)
        canonical(intent.request, 8192)
        if intent.transient_input is not None:
            transient = _transient_input(
                intent.transient_input, intent.request, intent.request_digest
            )
            if (
                data.get("execution_profile") != "one_shot"
                or transient.source_retention != data["operation"]["source_retention"]
                or intent.sensitivity != "ordinary"
                or intent.interactive_only
                or intent.boundary != "unreplayable"
            ):
                raise RepositoryValidationError("transient model disposition is not supported here")
        elif digest(intent.request) != intent.request_digest:
            raise RepositoryValidationError("action request digest mismatch")
        self._amount(intent.maximum)
        if intent.sensitivity not in {"ordinary", "sensitive"}:
            raise RepositoryValidationError("invalid sensitivity disposition")
        if not isinstance(intent.interactive_only, bool):
            raise RepositoryValidationError("interactive_only must be boolean")
        if intent.boundary not in {
            "internal_transaction",
            "downstream_key",
            "read_only",
            "unreplayable",
        }:
            raise RepositoryValidationError("unreviewed effect boundary")
        if intent.boundary == "downstream_key":
            _text(intent.downstream_key)
        if intent.task_id and not any(t["task_id"] == intent.task_id for t in data["tasks"]):
            _conflict("assignment_task_not_found")
        if intent.event_id:
            self._event(transaction, fence.owner_id, fence.assignment_id, intent.event_id)
        row = transaction.fetch_one(
            "SELECT id,data FROM persistent_assignment_action "
            "WHERE assignment_id=%s AND owner_user_id=%s AND action_key=%s",
            (fence.assignment_id, fence.owner_id, intent.action_key),
        )
        if row:
            old = plain(row["data"])
            if old["intent_digest"] != digest(intent):
                _conflict("assignment_idempotency_conflict")
            return _action_record(old)
        counts = transaction.fetch_one(
            "SELECT count(*) AS total,count(*) FILTER "
            "(WHERE state='proposed') AS pending FROM "
            "persistent_assignment_action WHERE assignment_id=%s",
            (fence.assignment_id,),
        )
        if counts["total"] >= 10000 or counts["pending"] >= 100:
            _conflict("assignment_history_capacity_exhausted")
        needs_approval = intent.sensitivity == "sensitive" or intent.interactive_only
        if needs_approval:
            expiry = _time(intent.approval_expires_at)
            now = _now(transaction)
            if expiry is None or not now < expiry <= now + timedelta(hours=24):
                raise RepositoryValidationError("approval expiry outside allowed bound")
        action = dict(
            action_id=str(uuid.uuid4()),
            assignment_id=fence.assignment_id,
            owner_id=fence.owner_id,
            intent=plain(intent),
            intent_digest=digest(intent),
            instruction_revision=fence.instruction_revision,
            control_epoch=fence.control_epoch,
            state="proposed" if needs_approval else "ready",
            result=None,
            attempts=[],
            decision=None,
            foreground_admission=None,
            reconciliation=None,
        )
        transaction.execute(
            "INSERT INTO persistent_assignment_action "
            "(id,assignment_id,owner_user_id,action_key,state,data) "
            "VALUES(%s,%s,%s,%s,%s,%s::jsonb)",
            (
                action["action_id"],
                fence.assignment_id,
                fence.owner_id,
                intent.action_key,
                action["state"],
                canonical(action),
            ),
        )
        if needs_approval:
            data["phase"] = "waiting_approval"
            self._activity(
                transaction,
                data,
                AssignmentActivityRecord(
                    "approval:" + action["action_id"],
                    "approval",
                    "Approval required",
                    "Review the exact proposed action before it can run.",
                    {"action_id": action["action_id"]},
                    notification_state="pending",
                ),
            )
            self._save(transaction, data)
        return _action_record(action)

    def get_action(self, query, *, owner_id, assignment_id, action_id):
        self._load(query, owner_id, assignment_id)
        data = self._action(
            query, owner_id, assignment_id, action_id, required=False, inspect_only=True
        )
        return _action_record(data) if data else None

    def get_action_by_key(self, query, *, owner_id, assignment_id, action_key):
        self._load(query, owner_id, assignment_id)
        _text(action_key)
        row = query.fetch_one(
            "SELECT data FROM persistent_assignment_action WHERE assignment_id=%s "
            "AND owner_user_id=%s AND action_key=%s",
            (assignment_id, owner_id, action_key),
        )
        return None if row is None else _action_record(plain(row["data"]))

    def link_interactive_proposal(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        action_id,
        expected_request_digest,
        proposal_id,
        expected_instruction_revision,
        expected_control_epoch,
    ):
        data = self._load(transaction, owner_id, assignment_id, lock=True)
        _version(data, expected_instruction_revision, expected_control_epoch)
        _text(proposal_id, 128)
        action = self._action(transaction, owner_id, assignment_id, action_id)
        if (
            data["lifecycle"] != "active"
            or action["state"] not in {"proposed", "approved"}
            or action["intent"]["request_digest"] != expected_request_digest
            or not action["intent"]["interactive_only"]
        ):
            _conflict("assignment_approval_invalid")
        prior = action.get("interactive_proposal_id")
        if prior is not None:
            if prior != proposal_id:
                _conflict("assignment_idempotency_conflict")
            return _action_record(action)
        proposal = transaction.fetch_one(
            "SELECT agent_id,verb,args_fingerprint,expires_at,status "
            "FROM remote_operation_proposal "
            "WHERE proposal_id=%s AND owner_user_id=%s FOR UPDATE",
            (proposal_id, owner_id),
        )
        request = action["intent"]["request"]
        arguments = {
            k: v for k, v in request.get("arguments", {}).items() if not str(k).startswith("_")
        }
        fingerprint = hashlib.sha256(
            json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if (
            proposal is None
            or proposal["status"] != "pending"
            or proposal["expires_at"] <= int(_now(transaction).timestamp())
            or proposal["agent_id"] != request.get("agent_id")
            or proposal["verb"] != request.get("tool_name")
            or proposal["args_fingerprint"] != fingerprint
        ):
            _conflict("assignment_approval_invalid")
        other = transaction.fetch_one(
            "SELECT id FROM persistent_assignment_action WHERE owner_user_id=%s "
            "AND data->>'interactive_proposal_id'=%s",
            (owner_id, proposal_id),
        )
        if other is not None:
            _conflict("assignment_idempotency_conflict")
        action["interactive_proposal_id"] = proposal_id
        return self._save_action(transaction, action)

    def get_action_for_interactive_proposal(self, query, *, owner_id, proposal_id):
        _text(owner_id)
        _text(proposal_id, 128)
        row = query.fetch_one(
            "SELECT data FROM persistent_assignment_action WHERE owner_user_id=%s "
            "AND data->>'interactive_proposal_id'=%s",
            (owner_id, proposal_id),
        )
        return None if row is None else _action_record(plain(row["data"]))

    def observe_interactive_proposal(self, transaction, *, owner_id, proposal_id):
        linked = self.get_action_for_interactive_proposal(
            transaction, owner_id=owner_id, proposal_id=proposal_id
        )
        if linked is None:
            return None
        data = self._load(transaction, owner_id, linked.assignment_id, lock=True)
        action = self._action(transaction, owner_id, linked.assignment_id, linked.action_id)
        row = transaction.fetch_one(
            "SELECT status,expires_at FROM remote_operation_proposal "
            "WHERE proposal_id=%s AND owner_user_id=%s FOR UPDATE",
            (proposal_id, owner_id),
        )
        if row is None:
            _conflict("assignment_approval_invalid")
        expired = row["expires_at"] <= int(_now(transaction).timestamp())
        if (row["status"] not in {"declined", "expired"} and not expired) or action[
            "state"
        ] not in {"proposed", "approved", "reserved"}:
            return _action_record(action)
        for attempt in action["attempts"]:
            if attempt["state"] == "reserved":
                self._release(data, attempt["maximum"])
                attempt["state"] = "failed_not_started"
        action["state"] = "declined" if row["status"] == "declined" else "invalidated"
        self._save_action(transaction, action)
        if data["lifecycle"] == "active":
            data.update(
                phase="waiting",
                next_wake_at=plain(_now(transaction)),
                wake_reason="approval_declined",
                wake_generation=data["wake_generation"] + 1,
            )
        self._save(transaction, data)
        return _action_record(action)

    def list_actions(self, query, *, owner_id, assignment_id, states=(), limit=100, after_id=None):
        self._load(query, owner_id, assignment_id)
        _integer(limit, 1, 100)
        if after_id is not None:
            _uuid(after_id)
        if len(states) > 12:
            raise RepositoryValidationError("too many action states")
        rows = query.fetch_all(
            "SELECT data FROM persistent_assignment_action WHERE assignment_id=%s "
            "AND owner_user_id=%s AND (%s::uuid IS NULL OR id>%s::uuid) "
            "AND (cardinality(%s::text[])=0 OR state=ANY(%s::text[])) "
            "ORDER BY id LIMIT %s",
            (assignment_id, owner_id, after_id, after_id, list(states), list(states), limit),
        )
        return tuple(_action_record(plain(row["data"])) for row in rows)

    def decide_action(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        action_id,
        expected_instruction_revision,
        expected_control_epoch,
        decision,
        expected_state_version=None,
    ):
        owner_active = self._lock_operation_owner(transaction, owner_id)
        data = self._load(transaction, owner_id, assignment_id, lock=True)
        one_shot = data.get("execution_profile") == "one_shot"
        if one_shot:
            _integer(expected_state_version, 1)
        _version(data, expected_instruction_revision, expected_control_epoch)
        action = self._action(transaction, owner_id, assignment_id, action_id)
        _uuid(decision.submission_id)
        _digest(decision.submission_digest)
        if action["decision"] is not None:
            if action["decision"] != plain(decision):
                _conflict("assignment_approval_invalid")
            return _action_record(action)
        if one_shot:
            _state_version(data, expected_state_version)
            if not owner_active:
                _conflict("assignment_owner_retired")
            self._validate_operation_continuation(transaction, data)
        self._approve_conditions(
            transaction,
            data,
            action,
            decision.proposal_digest,
            decision.permission_digest,
            decision.precondition_digest,
        )
        if action["state"] != "proposed" or decision.decision not in {"approve", "decline"}:
            _conflict("assignment_approval_invalid")
        action.update(
            decision=plain(decision),
            state="approved" if decision.decision == "approve" else "declined",
        )
        self._save_action(transaction, action)
        self._activity(
            transaction,
            data,
            AssignmentActivityRecord(
                "decision:" + decision.submission_id,
                "approval",
                "Action " + decision.decision,
                "",
                {"action_id": action_id},
            ),
        )
        self._save(transaction, data)
        return _action_record(action)

    @staticmethod
    def _approve_conditions(transaction, data, action, request_digest, permissions, preconditions):
        intent = action["intent"]
        if (
            data["lifecycle"] != "active"
            or action["instruction_revision"] != data["instruction_revision"]
            or action["control_epoch"] != data["control_epoch"]
            or request_digest != intent["request_digest"]
            or permissions != intent["permission_digest"]
            or preconditions != intent["precondition_digest"]
        ):
            _conflict("assignment_approval_invalid")
        if intent["sensitivity"] == "sensitive" or intent["interactive_only"]:
            expiry = _time(intent["approval_expires_at"])
            if expiry is None or expiry <= _now(transaction):
                _conflict("assignment_approval_invalid")

    def claim_for_approved_action(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        action_id,
        expected_request_digest,
        expected_instruction_revision,
        expected_control_epoch,
        interactive_receipt_id,
        submission_id,
        submission_digest,
        worker_id,
        lease_seconds=30,
        authority=None,
        expected_state_version=None,
    ):
        with transaction.savepoint("operation_approved_claim_" + uuid.uuid4().hex):
            selected = self._load(transaction, owner_id, assignment_id)
            one_shot = selected.get("execution_profile") == "one_shot"
            if one_shot:
                data = self._operation_claim_context(
                    transaction, owner_id, assignment_id, authority
                )
                _state_version(data, expected_state_version)
            else:
                data = self._load(transaction, owner_id, assignment_id, lock=True)
            _version(data, expected_instruction_revision, expected_control_epoch)
            _text(interactive_receipt_id)
            _uuid(submission_id)
            _digest(submission_digest)
            action = self._action(transaction, owner_id, assignment_id, action_id)
            self._approve_conditions(
                transaction,
                data,
                action,
                expected_request_digest,
                action["intent"]["permission_digest"],
                action["intent"]["precondition_digest"],
            )
            if action["state"] != "approved" or action["foreground_admission"] is not None:
                _conflict("assignment_approval_invalid")
            if data["lease_expires_at"] is not None:
                _conflict("assignment_claim_busy")
            claim = self._claim(transaction, data, worker_id, lease_seconds, action_id)
            action["foreground_admission"] = dict(
                submission_id=submission_id,
                submission_digest=submission_digest,
                receipt_id=interactive_receipt_id,
                claim_generation=claim.fence.claim_generation,
            )
            self._save_action(transaction, action)
            if one_shot:
                self._assert_operation_claim_current(transaction, data, authority)
            return claim

    def reserve_action(
        self,
        transaction,
        *,
        fence,
        action_id,
        attempt_id,
        expected_request_digest,
        maximum,
        quote_digest=None,
        quote_expires_at=None,
    ):
        data = self._fenced(transaction, fence, action_id=action_id)
        action = self._action(transaction, fence.owner_id, fence.assignment_id, action_id)
        _uuid(attempt_id)
        amount = self._amount(maximum)
        if action["intent"]["request_digest"] != expected_request_digest:
            _conflict("assignment_idempotency_conflict")
        if amount != action["intent"]["maximum"]:
            _conflict("assignment_reservation_exceeds_intent")
        if quote_digest != action["intent"]["quote_digest"] or _time(quote_expires_at) != _time(
            action["intent"]["quote_expires_at"]
        ):
            _conflict("assignment_cost_bound_unavailable")
        for attempt in action["attempts"]:
            if attempt["attempt_id"] == attempt_id:
                if attempt["maximum"] != amount or attempt["quote_digest"] != quote_digest:
                    _conflict("assignment_idempotency_conflict")
                return AssignmentActionReservation(
                    _action_record(action), attempt_id, maximum, False
                )
        if action["state"] not in {"ready", "approved", "failed_not_started", "failed"}:
            _conflict("assignment_action_uncertain")
        if action["state"] == "failed" and action["intent"]["boundary"] not in {
            "read_only",
            "downstream_key",
        }:
            _conflict("assignment_action_uncertain")
        if len(action["attempts"]) >= 1 + data["definition"]["limits"]["max_retries"]:
            _conflict("assignment_retry_exhausted")
        now = _now(transaction)
        self._day(data, now)
        if data["definition"]["limits"].get("spend_micro_units") is not None:
            _digest(quote_digest)
            if (
                _time(quote_expires_at) is None
                or _time(quote_expires_at) <= now
                or amount["spend_micro_units"] is None
            ):
                _conflict("assignment_cost_bound_unavailable")
        self._reserve(data, amount)
        action["attempts"].append(
            dict(
                attempt_id=attempt_id,
                state="reserved",
                maximum=amount,
                quote_digest=quote_digest,
                quote_expires_at=plain(quote_expires_at),
                dispatch_token=None,
                binding=None,
                outcome=None,
            )
        )
        action["state"] = "reserved"
        self._save_action(transaction, action)
        self._save(transaction, data)
        return AssignmentActionReservation(_action_record(action), attempt_id, maximum, True)

    def start_action(
        self,
        transaction,
        *,
        fence,
        action_id,
        attempt_id,
        expected_request_digest,
        current_permission_digest,
        current_precondition_digest,
        binding,
        interactive_receipt_id=None,
    ):
        data = self._fenced(transaction, fence, action_id=action_id)
        action = self._action(transaction, fence.owner_id, fence.assignment_id, action_id)
        self._approve_conditions(
            transaction,
            data,
            action,
            expected_request_digest,
            current_permission_digest,
            current_precondition_digest,
        )
        if data["operation_binding"] != plain(binding):
            _conflict("assignment_operation_conflict")
        attempt = self._attempt(action, attempt_id)
        if attempt["state"] != "reserved" or action["state"] != "reserved":
            _conflict("assignment_action_already_started")
        needs_approval = (
            action["intent"]["sensitivity"] == "sensitive" or action["intent"]["interactive_only"]
        )
        if needs_approval and (
            action["decision"] is None
            or action["decision"]["decision"] != "approve"
            or action.get("approval_consumed_at") is not None
        ):
            _conflict("assignment_approval_invalid")
        if action["intent"]["interactive_only"]:
            admission = action["foreground_admission"]
            if (
                not admission
                or admission["receipt_id"] != interactive_receipt_id
                or admission["claim_generation"] != fence.claim_generation
            ):
                _conflict("assignment_approval_invalid")
        if data["definition"]["limits"].get("spend_micro_units") is not None and _time(
            attempt["quote_expires_at"]
        ) <= _now(transaction):
            _conflict("assignment_cost_bound_unavailable")
        token = str(uuid.uuid4())
        if needs_approval:
            action["approval_consumed_at"] = plain(_now(transaction))
        attempt.update(state="started", dispatch_token=token, binding=plain(binding))
        if data.get("execution_profile") == "one_shot":
            attempt["assignment_fence"] = plain(fence)
        action["state"] = "started"
        self._save_action(transaction, action)
        return AssignmentDispatchPermit(
            action_id, attempt_id, token, expected_request_digest, binding
        )

    @staticmethod
    def _attempt(action, attempt_id):
        for attempt in action["attempts"]:
            if attempt["attempt_id"] == attempt_id:
                return attempt
        raise RepositoryNotFoundError("assignment attempt not found")

    def record_action_outcome(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        action_id,
        attempt_id,
        dispatch_token,
        expected_request_digest,
        outcome,
        result_fence=None,
        result_binding=None,
        result_authority=None,
    ):
        owner_active = self._lock_operation_owner(transaction, owner_id)
        selected = self._load(transaction, owner_id, assignment_id)
        authority_current = (
            owner_active and self._lock_execution_authority(transaction, selected, result_authority)
            if selected.get("execution_profile") == "one_shot"
            else owner_active
        )
        data = self._load(transaction, owner_id, assignment_id, lock=True)
        one_shot = data.get("execution_profile") == "one_shot"
        current = not one_shot or self._result_context_current(
            transaction,
            data,
            action_id,
            authority_current
            and (
                self._execution_authority_selection(data)
                == self._execution_authority_selection(selected)
            ),
            result_fence,
            result_binding,
            result_authority,
        )
        action = self._action(transaction, owner_id, assignment_id, action_id)
        attempt = self._attempt(action, attempt_id)
        if one_shot:
            current = (
                current
                and self._result_context_current(
                    transaction,
                    data,
                    action_id,
                    True,
                    result_fence,
                    result_binding,
                    result_authority,
                )
                and attempt.get("assignment_fence") == plain(result_fence)
                and attempt["binding"] == plain(result_binding)
            )
        if (
            not dispatch_token
            or attempt["dispatch_token"] != dispatch_token
            or expected_request_digest != action["intent"]["request_digest"]
        ):
            _conflict("assignment_result_conflict")
        if outcome.outcome not in {"succeeded", "failed", "uncertain", "failed_not_started"}:
            raise RepositoryValidationError("invalid action outcome")
        _digest(outcome.result_digest)
        canonical(outcome.result, 8192)
        disposition = None
        if outcome.result_disposition is not None:
            disposition = _result_disposition(outcome.result_disposition, outcome.result)
        transient = action["intent"].get("transient_input")
        if transient is not None:
            _transient_receipt(disposition, outcome.evidence_reference)
        signature_value = plain(outcome)
        signature_value.pop("result")
        signature = digest(signature_value)
        if one_shot and attempt.get("settlement_signature") == signature:
            return self._settlement_record(action, current)
        if one_shot and not current:
            outcome = replace(
                outcome,
                result={},
                result_disposition=AssignmentResultDisposition(
                    available=False,
                    reason="stale_execution",
                    binding_key_id=disposition.binding_key_id if disposition else None,
                ),
            )
        if attempt["outcome"] is not None:
            if attempt["outcome"] == plain(outcome):
                return _action_record(action)
            if attempt["outcome"]["outcome"] != "uncertain" or outcome.outcome == "uncertain":
                _conflict("assignment_result_conflict")
            attempt["uncertain_observation"] = attempt["outcome"]
        if attempt["state"] not in {"started", "uncertain"}:
            _conflict("assignment_result_conflict")
        if outcome.outcome == "failed_not_started":
            raise RepositoryValidationError("issued permits require observed outcomes, not refunds")
        actual = self._amount(outcome.actual) if outcome.actual is not None else attempt["maximum"]
        maximum = attempt["maximum"]
        if maximum["spend_micro_units"] is not None and actual["spend_micro_units"] is None:
            actual["spend_micro_units"] = maximum["spend_micro_units"]
            actual["currency"] = maximum["currency"]
        if actual["currency"] != maximum["currency"] and maximum["currency"] is not None:
            _conflict("assignment_result_currency_conflict")
        if outcome.outcome != "uncertain":
            self._release(data, maximum)
            self._day(data, _now(transaction))
            for key in (*_DIMENSIONS, "spend_micro_units"):
                if actual.get(key) is not None:
                    for bucket in ("spent", "daily"):
                        data["usage"][bucket][key] = data["usage"][bucket].get(key, 0) + actual[key]
            if actual["spend_micro_units"] is not None:
                data["usage"]["money_status"] = "reported"
        attempt.update(state=outcome.outcome, outcome=plain(outcome))
        if one_shot:
            attempt["settlement_signature"] = signature
        action.update(state=outcome.outcome, result=_outcome_projection(outcome))
        if current and outcome.outcome == "uncertain":
            data["phase"] = "reconciliation"
        elif current and data["lifecycle"] == "active":
            data["wake_generation"] += 1
        self._save_action(transaction, action)
        self._save(transaction, data)
        return _action_record(action)

    def _result_context_current(
        self,
        transaction,
        data,
        action_id,
        authority_current,
        fence,
        binding,
        authority,
    ):
        if not _executable(data) or not authority_current or fence is None or binding is None:
            return False
        if not isinstance(fence, AssignmentFence) or not isinstance(
            binding, AssignmentOperationBinding
        ):
            raise RepositoryValidationError("typed result fences required")
        if (
            fence.owner_id != data["owner_id"]
            or fence.assignment_id != data["assignment_id"]
            or data["operation_binding"] != plain(binding)
        ):
            return False
        try:
            self._assert_bound_admission(transaction, data, binding)
            self._fenced(transaction, fence, action_id=action_id)
            self._validate_operation_continuation(transaction, data)
            return self._lock_execution_authority(transaction, data, authority)
        except (RepositoryConflictError, RepositoryNotFoundError):
            return False

    @staticmethod
    def _settlement_record(action, current):
        if current or action["result"] is None:
            return _action_record(action)
        action = plain(action)
        result = action["result"]
        prior = result.get("result_disposition") or {}
        result.update(
            result={},
            result_available=False,
            reacquisition_reason="stale_execution",
            result_disposition=plain(
                AssignmentResultDisposition(
                    available=False,
                    reason="stale_execution",
                    binding_key_id=prior.get("binding_key_id"),
                )
            ),
        )
        return _action_record(action)

    def release_unstarted_action(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        action_id,
        attempt_id,
        expected_request_digest,
        reason_code,
    ):
        _text(reason_code, 128)
        data = self._load(transaction, owner_id, assignment_id, lock=True)
        action = self._action(transaction, owner_id, assignment_id, action_id)
        if action["intent"]["request_digest"] != expected_request_digest:
            _conflict("assignment_idempotency_conflict")
        attempt = self._attempt(action, attempt_id)
        if attempt["state"] == "failed_not_started":
            return _action_record(action)
        if attempt["state"] != "reserved" or attempt["dispatch_token"] is not None:
            _conflict("assignment_action_uncertain")
        self._release(data, attempt["maximum"])
        attempt["state"] = "failed_not_started"
        action["state"] = "failed_not_started"
        self._save_action(transaction, action)
        self._save(transaction, data)
        return _action_record(action)

    def _prepare_action_reconciliation(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        action_id,
        expected_instruction_revision,
        expected_control_epoch,
        decision,
        expected_state_version,
        authority,
    ):
        if type(decision) is not AssignmentActionReconciliation:
            raise RepositoryValidationError("typed reconciliation decision required")
        _uuid(decision.submission_id)
        _digest(decision.submission_digest)
        _digest(decision.prior_result_digest)
        _text(decision.evidence_reference, 2048)
        if type(decision.decision) is not str or decision.decision not in {
            "confirmed_applied",
            "confirmed_not_applied",
        }:
            raise RepositoryValidationError("invalid reconciliation decision")
        owner_active = self._lock_operation_owner(transaction, owner_id)
        selected = self._load(transaction, owner_id, assignment_id)
        one_shot = selected.get("execution_profile") == "one_shot"
        authority_current = (
            owner_active and self._lock_execution_authority(transaction, selected, authority)
            if one_shot
            else owner_active
        )
        data = self._load(transaction, owner_id, assignment_id, lock=True)
        if one_shot:
            _integer(expected_state_version, 1)
        _version(data, expected_instruction_revision, expected_control_epoch)
        if one_shot:
            self._purge_blockers(transaction, data)
            authority_current = authority_current and (
                self._execution_authority_selection(selected)
                == self._execution_authority_selection(data)
            )
        action = self._action(transaction, owner_id, assignment_id, action_id)
        if one_shot and self._known_action(transaction, owner_id, assignment_id, action_id) is None:
            raise RepositoryDataError("invalid reconciliation action evidence")
        replayed = action["reconciliation"] is not None
        if replayed:
            if action["reconciliation"] != plain(decision):
                _conflict("assignment_idempotency_conflict")
        else:
            if one_shot:
                _state_version(data, expected_state_version)
            if (
                action["state"] != "uncertain"
                or (action["result"] or {}).get("result_digest") != decision.prior_result_digest
                or not action["attempts"]
                or action["attempts"][-1]["state"] != "uncertain"
                or action["attempts"][-1]["dispatch_token"] is None
            ):
                _conflict("assignment_result_conflict")
        return data, action, owner_active, authority_current, replayed

    def prepare_action_reconciliation(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        action_id,
        expected_instruction_revision,
        expected_control_epoch,
        decision,
        expected_state_version=None,
        authority=None,
    ):
        data, action, _, _, replayed = self._prepare_action_reconciliation(
            transaction,
            owner_id=owner_id,
            assignment_id=assignment_id,
            action_id=action_id,
            expected_instruction_revision=expected_instruction_revision,
            expected_control_epoch=expected_control_epoch,
            decision=decision,
            expected_state_version=expected_state_version,
            authority=authority,
        )
        return AssignmentActionReconciliationPreparation(
            _record(data), _action_record(action), replayed
        )

    def reconcile_action(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        action_id,
        expected_instruction_revision,
        expected_control_epoch,
        decision,
        expected_state_version=None,
        authority=None,
    ):
        with transaction.savepoint("assignment_reconcile_" + uuid.uuid4().hex):
            data, action, owner_active, authority_current, replayed = (
                self._prepare_action_reconciliation(
                    transaction,
                    owner_id=owner_id,
                    assignment_id=assignment_id,
                    action_id=action_id,
                    expected_instruction_revision=expected_instruction_revision,
                    expected_control_epoch=expected_control_epoch,
                    decision=decision,
                    expected_state_version=expected_state_version,
                    authority=authority,
                )
            )
            if replayed:
                return _action_record(action)
            attempt = action["attempts"][-1]
            self._release(data, attempt["maximum"])
            self._day(data, _now(transaction))
            for key in (*_DIMENSIONS, "spend_micro_units"):
                if attempt["maximum"].get(key) is not None:
                    for bucket in ("spent", "daily"):
                        data["usage"][bucket][key] = (
                            data["usage"][bucket].get(key, 0) + attempt["maximum"][key]
                        )
            action["reconciliation"] = plain(decision)
            action["state"] = (
                "succeeded" if decision.decision == "confirmed_applied" else "failed_not_started"
            )
            attempt["state"] = action["state"]
            action["result"] = {
                "outcome": "reconciled_applied"
                if decision.decision == "confirmed_applied"
                else "reconciled_not_applied",
                "result_digest": digest(["reconciliation", plain(decision)]),
                "result": {},
                "result_available": False,
                "evidence_reference": decision.evidence_reference,
                "reconciliation": {
                    "decision": decision.decision,
                    "prior_result_digest": decision.prior_result_digest,
                },
            }
            self._save_action(transaction, action)
            if data["lifecycle"] == "active":
                continuation = True
                if data.get("execution_profile") == "one_shot":
                    continuation = self._reconciliation_continuation(
                        transaction, data, owner_active, authority_current, authority
                    )
                if continuation:
                    if data.get("execution_profile") == "one_shot":
                        data.update(next_retry_at=None, safe_error_code=None)
                    data.update(
                        phase="waiting",
                        next_wake_at=plain(_now(transaction)),
                        wake_reason="reconciled",
                        wake_generation=data["wake_generation"] + 1,
                    )
            self._save(transaction, data)
            return _action_record(action)

    def _reconciliation_continuation(
        self,
        transaction,
        data,
        owner_active,
        authority_current,
        authority,
    ):
        actions, _, held = self._purge_blockers(transaction, data)
        if held or any(action["state"] in {"proposed", "approved"} for action in actions):
            data.update(phase="reconciliation", next_wake_at=None, next_retry_at=None)
            return False
        if data["operation"].get("control", {}).get("wait") is not None:
            data.update(phase="awaiting_event", next_wake_at=None, next_retry_at=None)
            return False
        if data["phase"] == "budget_exhausted":
            data.update(next_wake_at=None, next_retry_at=None)
            return False
        try:
            if _time(data["operation"]["deadline_at"]) <= _now(transaction):
                _conflict("assignment_deadline_exceeded")
            if not owner_active:
                _conflict("assignment_owner_retired")
            self._validate_operation_continuation(transaction, data)
            if not authority_current or not self._lock_execution_authority(
                transaction, data, authority
            ):
                _conflict("assignment_authorization_unavailable")
        except RepositoryConflictError as exc:
            data.update(
                phase="failed"
                if exc.code == "assignment_deadline_exceeded"
                else "waiting_authorization",
                next_wake_at=None,
                next_retry_at=None,
                safe_error_code=exc.code,
            )
            self._clear_claim(data)
            if exc.code == "assignment_deadline_exceeded":
                self._terminal_operation_failure(transaction, data)
            return False
        return True

    @staticmethod
    def _event(transaction, owner_id, assignment_id, event_id):
        _uuid(event_id)
        row = transaction.fetch_one(
            "SELECT data FROM persistent_assignment_event WHERE id=%s "
            "AND assignment_id=%s AND owner_user_id=%s",
            (event_id, assignment_id, owner_id),
        )
        if row is None:
            raise RepositoryNotFoundError("assignment event not found")
        return plain(row["data"])

    def record_source_batch(self, transaction, *, fence, expected_state_version, batch):
        data = self._fenced(transaction, fence)
        _text(batch.batch_key)
        _digest(batch.batch_digest)
        _digest(batch.configuration_digest)
        _digest(batch.expected_cursor_digest)
        canonical(batch, 65536)
        if len(batch.events) > 100:
            raise RepositoryValidationError("source batch exceeds item bound")
        batch_signature = digest(batch)
        prior = data["source_batches"].get(batch.batch_key)
        if prior is not None:
            if prior["signature"] != batch_signature:
                _conflict("assignment_idempotency_conflict")
            return _record(data), tuple(
                AssignmentSourceEvent(
                    **self._event(transaction, fence.owner_id, fence.assignment_id, event_id)
                )
                for event_id in prior["event_ids"]
            )
        if data["state_version"] != expected_state_version:
            _conflict("assignment_revision_conflict")
        if batch.configuration_digest != digest(
            data["definition"]["source"]
        ) or batch.expected_cursor_digest != digest(data["checkpoint"].get("cursor")):
            _conflict("assignment_source_cursor_conflict")
        if len(data["source_batches"]) >= 32:
            data["source_batches"].pop(next(iter(data["source_batches"])))
        count = transaction.fetch_one(
            "SELECT count(*) AS n FROM persistent_assignment_event WHERE assignment_id=%s",
            (fence.assignment_id,),
        )["n"]
        results = []
        seen = set()
        for event in batch.events:
            _uuid(event.event_id)
            for key in (event.source_key, event.item_key, event.source_revision):
                _text(key)
            if event.source_key != batch.source_key or event.event_id in seen:
                raise RepositoryValidationError("batch event identity invalid")
            seen.add(event.event_id)
            _digest(event.identity_digest)
            _digest(event.context_digest)
            canonical(event.context, 8192)
            if event.context_digest != digest(event.context) or event.disposition != "pending":
                raise RepositoryValidationError("source context or initial state invalid")
            row = transaction.fetch_one(
                "SELECT data FROM persistent_assignment_event "
                "WHERE assignment_id=%s AND owner_user_id=%s AND source_key=%s "
                "AND item_key=%s AND source_revision=%s",
                (
                    fence.assignment_id,
                    fence.owner_id,
                    event.source_key,
                    event.item_key,
                    event.source_revision,
                ),
            )
            if row:
                old = plain(row["data"])
                if (
                    old["context_digest"] != event.context_digest
                    or old["identity_digest"] != event.identity_digest
                ):
                    _conflict("assignment_idempotency_conflict")
                results.append(AssignmentSourceEvent(**old))
                continue
            if count >= 10000:
                _conflict("assignment_history_capacity_exhausted")
            count += 1
            transaction.execute(
                "INSERT INTO persistent_assignment_event "
                "(id,assignment_id,owner_user_id,source_key,item_key,source_revision,state,data) "
                "VALUES(%s,%s,%s,%s,%s,%s,'pending',%s::jsonb)",
                (
                    event.event_id,
                    fence.assignment_id,
                    fence.owner_id,
                    event.source_key,
                    event.item_key,
                    event.source_revision,
                    canonical(event),
                ),
            )
            results.append(event)
        data["source_batches"][batch.batch_key] = {
            "signature": batch_signature,
            "event_ids": [event.event_id for event in results],
        }
        data["checkpoint"].update(
            cursor=plain(batch.next_cursor),
            source_configuration_digest=batch.configuration_digest,
            last_batch_key=batch.batch_key,
        )
        data["last_check_at"] = plain(_now(transaction))
        return self._save(transaction, data), tuple(results)

    def list_events(
        self, query, *, owner_id, assignment_id, disposition=None, limit=100, after_id=None
    ):
        self._load(query, owner_id, assignment_id)
        _integer(limit, 1, 100)
        if after_id is not None:
            _uuid(after_id)
        rows = query.fetch_all(
            "SELECT data FROM persistent_assignment_event WHERE assignment_id=%s "
            "AND owner_user_id=%s AND (%s::text IS NULL OR state=%s) "
            "AND (%s::uuid IS NULL OR id>%s::uuid) ORDER BY id LIMIT %s",
            (assignment_id, owner_id, disposition, disposition, after_id, after_id, limit),
        )
        return tuple(AssignmentSourceEvent(**plain(row["data"])) for row in rows)

    def put_task_plan(
        self, transaction, *, fence, expected_state_version, plan_key, plan_digest, tasks
    ):
        data = self._fenced(transaction, fence)
        _text(plan_key)
        _digest(plan_digest)
        signature = digest(tasks)
        prior = data["plans"].get(plan_key)
        if prior:
            if prior != [plan_digest, signature]:
                _conflict("assignment_idempotency_conflict")
            return _record(data)
        if data["state_version"] != expected_state_version:
            _conflict("assignment_revision_conflict")
        limits = data["definition"]["limits"]
        if not 1 <= len(tasks) <= limits["max_tasks"] or len(data["plans"]) >= 256:
            _conflict("assignment_history_capacity_exhausted")
        for old in data["tasks"]:
            if old["state"] not in {"completed", "failed", "cancelled", "superseded"}:
                _conflict("assignment_task_plan_active")
            if old["state"] == "completed" and not old["incorporated_by"]:
                _conflict("assignment_task_result_unincorporated")
        values = plain(tasks)
        nodes = {node["task_id"]: node for node in values}
        if len(nodes) != len(tasks):
            raise RepositoryValidationError("duplicate task identity")
        for node in values:
            _text(node["task_id"], 128)
            _text(node["title"], 256)
            _text(node["instruction"], 4096)
            if (
                node["instruction_revision"] != fence.instruction_revision
                or node["plan_key"] != plan_key
                or node["state"] != "pending"
                or node["attempt_count"] != 0
                or node["result_digest"] is not None
                or node["incorporated_by"]
                or node["task_generation"] != 0
            ):
                raise RepositoryValidationError("new task has invalid initial authority/state")
            _integer(node["depth"], 0, limits["max_depth"])
            dependencies = node["depends_on"]
            if len(dependencies) > 8 or len(set(dependencies)) != len(dependencies):
                raise RepositoryValidationError("invalid dependencies")
            if any(dep not in nodes or dep == node["task_id"] for dep in dependencies):
                raise RepositoryValidationError("missing or self dependency")
            parent = nodes.get(node["parent_task_id"]) if node["parent_task_id"] else None
            if node["parent_task_id"] and (parent is None or parent["task_id"] == node["task_id"]):
                raise RepositoryValidationError("invalid task parent")
            allowed = parent["allowed_tools"] if parent else data["definition"]["allowed_tools"]
            if not set(node["allowed_tools"]).issubset(allowed):
                raise RepositoryValidationError("child authority exceeds parent")
            if node["depth"] != (parent["depth"] + 1 if parent else 0):
                raise RepositoryValidationError("invalid task depth")
            if sum(n["parent_task_id"] == node["task_id"] for n in values) > 5:
                raise RepositoryValidationError("task fanout exceeds bound")
            if node["event_id"]:
                self._event(transaction, fence.owner_id, fence.assignment_id, node["event_id"])
        visiting, visited = set(), set()

        def visit(task_id):
            if task_id in visiting:
                raise RepositoryValidationError("task graph cycle")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dep in nodes[task_id]["depends_on"]:
                visit(dep)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in nodes:
            visit(task_id)
        canonical(values, 196608)
        data["plans"][plan_key] = [plan_digest, signature]
        data.update(tasks=values, phase="investigating")
        return self._save(transaction, data)

    @staticmethod
    def _task(data, task_id):
        for task in data["tasks"]:
            if task["task_id"] == task_id:
                return task
        raise RepositoryNotFoundError("assignment task not found")

    def claim_task(self, transaction, *, fence, task_id, expected_task_generation):
        data = self._fenced(transaction, fence)
        task = self._task(data, task_id)
        limits = data["definition"]["limits"]
        if task["state"] != "pending" or task["task_generation"] != expected_task_generation:
            _conflict("assignment_task_claim_stale")
        if any(self._task(data, dep)["state"] != "completed" for dep in task["depends_on"]):
            _conflict("assignment_task_dependency_invalid")
        if task["attempt_count"] >= 1 + limits["max_retries"]:
            _conflict("assignment_retry_exhausted")
        if sum(t["state"] == "running" for t in data["tasks"]) >= limits["max_concurrent_tasks"]:
            _conflict("assignment_task_capacity_exhausted")
        task.update(
            state="running",
            attempt_count=task["attempt_count"] + 1,
            task_generation=task["task_generation"] + 1,
            claim_generation=fence.claim_generation,
        )
        self._save(transaction, data)
        return AssignmentTaskClaim(
            fence, task_id, task["task_generation"], task["attempt_count"], AssignmentTask(**task)
        )

    def complete_task(self, transaction, *, claim, result):
        data = self._fenced(transaction, claim.fence)
        task = self._task(data, claim.task_id)
        _digest(result.result_digest)
        canonical(result, 10000)
        if result.state not in {"completed", "failed", "cancelled", "reconciliation"}:
            raise RepositoryValidationError("invalid task result state")
        if task["task_generation"] != claim.task_generation:
            _conflict("assignment_task_claim_stale")
        if task["state"] != "running":
            if (
                task["result_digest"] == result.result_digest
                and task["bounded_result"] == result.bounded_result
                and task["state"] == result.state
                and task["provenance"] == plain(result.provenance)
            ):
                return _record(data)
            _conflict("assignment_result_conflict")
        if len(result.bounded_result.encode()) > 8192:
            raise RepositoryValidationError("task result exceeds bound")
        unresolved = transaction.fetch_one(
            "SELECT count(*) AS n FROM persistent_assignment_action "
            "WHERE assignment_id=%s AND data->'intent'->>'task_id'=%s "
            "AND state IN ('started','uncertain','reserved','proposed','approved')",
            (claim.fence.assignment_id, claim.task_id),
        )["n"]
        if unresolved and result.state == "completed":
            _conflict("assignment_action_uncertain")
        task.update(
            state=result.state,
            result_digest=result.result_digest,
            bounded_result=result.bounded_result,
            provenance=plain(result.provenance),
        )
        data["wake_generation"] += 1
        return self._save(transaction, data)

    @staticmethod
    def _validate_operation_completion(data, completion):
        if not isinstance(completion.checkpoint, Mapping):
            raise RepositoryValidationError("checkpoint object required")
        _integer(completion.checkpoint.get("schema_version", 1), 1, 1)
        if type(completion.completed) is not bool:
            raise RepositoryValidationError("completed must be boolean")
        if completion.terminal_outcome is not None and (
            not isinstance(completion.terminal_outcome, str)
            or completion.terminal_outcome not in {"completed", "failed"}
        ):
            raise RepositoryValidationError("invalid terminal outcome")
        if (
            completion.terminal_outcome is not None or completion.result_reference is not None
        ) and not completion.completed:
            raise RepositoryValidationError("result requires terminal completion")
        if completion.result_reference is not None:
            _text(completion.result_reference, 512)
        if completion.event_wait is not None:
            wait = plain(completion.event_wait)
            if (
                completion.completed
                or completion.phase != "awaiting_event"
                or not isinstance(wait, dict)
                or set(wait) != {"event_key", "source_revision"}
            ):
                raise RepositoryValidationError("invalid event wait completion")
            _text(wait["event_key"], 128)
            _integer(wait["source_revision"])
            state = _operation_control(data["operation"])
            if wait["source_revision"] < state["watermarks"].get(wait["event_key"], 0):
                _conflict("assignment_event_revision_conflict")
            if wait["event_key"] not in state["watermarks"] and len(state["watermarks"]) >= 64:
                _conflict("assignment_history_capacity_exhausted")
        elif completion.phase == "awaiting_event":
            raise RepositoryValidationError("event wait required")
        if (
            not completion.completed
            and completion.phase == "waiting"
            and completion.next_wake_at is None
        ):
            raise RepositoryValidationError("one-shot yield requires an explicit due time")
        if completion.wake_reason == "cadence" and not completion.completed:
            raise RepositoryValidationError("one-shot completion cannot recur")

    @staticmethod
    def _schedule_operation_completion(data, completion, now):
        operation = data["operation"]
        data.update(next_wake_at=None, next_retry_at=None)
        if completion.completed:
            return
        if completion.event_wait is not None:
            state = _operation_control(operation)
            state["wait"] = plain(completion.event_wait)
            state["watermarks"].setdefault(state["wait"]["event_key"], 0)
        if completion.phase == "failed":
            data["consecutive_failures"] += 1
            if data["consecutive_failures"] > data["definition"]["limits"]["max_retries"]:
                data["safe_error_code"] = "assignment_retry_exhausted"
                return
            due = now + timedelta(seconds=(5, 15, 45)[data["consecutive_failures"] - 1])
            if completion.next_wake_at is not None:
                due = max(due, _time(completion.next_wake_at))
        elif completion.phase == "waiting":
            due = max(now, _time(completion.next_wake_at))
        else:
            return
        if _time(operation["authority"]["expires_at"]) <= due:
            data.update(
                phase="waiting_authorization",
                safe_error_code="assignment_authorization_unavailable",
            )
        elif _time(operation["deadline_at"]) <= due:
            data.update(phase="failed", safe_error_code="assignment_deadline_exceeded")
        else:
            data["next_wake_at"] = plain(due)
            if completion.phase == "failed":
                data["next_retry_at"] = plain(due)

    def _terminal_operation_failure(self, transaction, data):
        if data["phase"] != "failed" or data["next_wake_at"] is not None:
            return
        actions, _, held = self._purge_blockers(transaction, data)
        if held or any(action["state"] in {"proposed", "approved"} for action in actions):
            data["phase"] = "reconciliation"
            return
        data["lifecycle"] = "completed"
        data["operation"]["terminal_outcome"] = "failed"
        for task in data["tasks"]:
            if task["state"] in {"pending", "running"}:
                task["state"] = "cancelled"
                task["task_generation"] += 1

    def finish_episode(self, transaction, *, fence, completion):
        raw = self._load(transaction, fence.owner_id, fence.assignment_id, lock=True)
        _digest(completion.completion_digest)
        last = raw.get("last_completion")
        signature_value = plain(completion)
        for key in ("terminal_outcome", "result_reference", "event_wait"):
            if signature_value[key] is None:
                signature_value.pop(key)
        signature = digest(signature_value)
        if last and last["claim_generation"] == fence.claim_generation:
            if (
                last["signature"] != signature
                or raw["control_epoch"] != fence.control_epoch
                or last["claim_token"] != fence.claim_token
            ):
                _conflict("assignment_result_conflict")
            return _record(raw)
        data = self._fenced(transaction, fence, action_id=raw.get("approved_action_id"))
        _state_version(data, completion.expected_state_version)
        if completion.phase not in _PHASES:
            raise RepositoryValidationError("invalid assignment phase")
        one_shot = data.get("execution_profile") == "one_shot"
        if not one_shot and (
            completion.phase == "awaiting_event"
            or any(
                value is not None
                for value in (
                    completion.event_wait,
                    completion.terminal_outcome,
                    completion.result_reference,
                )
            )
        ):
            raise RepositoryValidationError("one-shot completion required")
        if one_shot:
            self._validate_operation_completion(data, completion)
        if transaction.fetch_one(
            "SELECT count(*) AS n FROM persistent_assignment_action "
            "WHERE assignment_id=%s AND state='started'",
            (fence.assignment_id,),
        )["n"]:
            _conflict("assignment_action_in_flight")
        if (
            one_shot
            and not completion.completed
            and completion.phase in {"waiting", "failed", "awaiting_event"}
            and transaction.fetch_one(
                "SELECT count(*) AS n FROM persistent_assignment_action WHERE assignment_id=%s "
                "AND state IN ('uncertain','proposed','approved')",
                (fence.assignment_id,),
            )["n"]
        ):
            _conflict("assignment_unfinished_work")
        canonical(completion.checkpoint, 65536)
        for key in ("cursor", "source_configuration_digest", "last_batch_key"):
            if (
                key in data["checkpoint"]
                and plain(completion.checkpoint.get(key)) != data["checkpoint"][key]
            ):
                _conflict("assignment_source_cursor_conflict")
        for reference in completion.incorporations:
            task = self._task(data, reference["task_id"])
            parent = reference["parent_task_id"]
            if parent != (task["parent_task_id"] or "__assignment__"):
                _conflict("assignment_result_parent_conflict")
            if task["state"] != "completed" or task["result_digest"] != reference["result_digest"]:
                _conflict("assignment_result_conflict")
            old = task["incorporated_by"].get(parent)
            if old is not None and old != reference["result_digest"]:
                _conflict("assignment_result_conflict")
            task["incorporated_by"][parent] = reference["result_digest"]
        for receipt in completion.event_receipts:
            event = self._event(
                transaction, fence.owner_id, fence.assignment_id, receipt["event_id"]
            )
            if receipt["disposition"] not in {"completed", "irrelevant"}:
                raise RepositoryValidationError("invalid event completion")
            _digest(receipt["result_digest"])
            if event["disposition"] in {"completed", "irrelevant"} and (
                event["disposition"] != receipt["disposition"]
                or event["result_digest"] != receipt["result_digest"]
            ):
                _conflict("assignment_result_conflict")
            if any(
                t["event_id"] == event["event_id"]
                and t["state"] not in {"completed", "failed", "cancelled"}
                for t in data["tasks"]
            ):
                _conflict("assignment_task_dependency_invalid")
            if transaction.fetch_one(
                "SELECT count(*) AS n FROM persistent_assignment_action "
                "WHERE assignment_id=%s AND data->'intent'->>'event_id'=%s "
                "AND state IN ('reserved','started','uncertain','proposed','approved')",
                (fence.assignment_id, event["event_id"]),
            )["n"]:
                _conflict("assignment_action_uncertain")
            event.update(disposition=receipt["disposition"], result_digest=receipt["result_digest"])
            transaction.execute(
                "UPDATE persistent_assignment_event SET data=%s::jsonb,state=%s "
                "WHERE id=%s AND assignment_id=%s AND owner_user_id=%s",
                (
                    canonical(event),
                    event["disposition"],
                    event["event_id"],
                    fence.assignment_id,
                    fence.owner_id,
                ),
            )
        if completion.completed:
            unresolved = transaction.fetch_one(
                "SELECT count(*) AS n FROM persistent_assignment_action "
                "WHERE assignment_id=%s AND state IN "
                "('reserved','started','uncertain','proposed','approved')",
                (fence.assignment_id,),
            )["n"]
            if unresolved or any(
                t["state"] in {"pending", "running", "reconciliation"} for t in data["tasks"]
            ):
                _conflict("assignment_unfinished_work")
            data["lifecycle"] = "completed"
            if one_shot:
                data["operation"]["terminal_outcome"] = completion.terminal_outcome or "completed"
                if completion.result_reference is not None:
                    data["operation"]["result_reference"] = completion.result_reference
        now = _now(transaction)
        data.update(
            checkpoint=plain(completion.checkpoint),
            phase=completion.phase,
            safe_error_code=completion.safe_error_code,
            wake_reason=completion.wake_reason,
            last_completion={
                "claim_generation": fence.claim_generation,
                "claim_token": fence.claim_token,
                "signature": signature,
            },
        )
        if one_shot:
            self._schedule_operation_completion(data, completion, now)
        elif completion.phase in {"waiting", "failed"} and not completion.completed:
            due = _time(completion.next_wake_at) or now + timedelta(
                seconds=data["definition"]["limits"]["cadence_seconds"]
            )
            if data["last_check_at"] and completion.wake_reason == "cadence":
                due = max(
                    due,
                    _time(data["last_check_at"])
                    + timedelta(seconds=data["definition"]["limits"]["cadence_seconds"]),
                )
            if data["wake_generation"] > data["claimed_wake_generation"] and any(
                t["state"] == "completed" and not t["incorporated_by"] for t in data["tasks"]
            ):
                due = now
            data["next_wake_at"] = plain(max(due, now))
        else:
            data["next_wake_at"] = None
        if not one_shot and completion.phase == "failed":
            data["consecutive_failures"] += 1
            if data["consecutive_failures"] > data["definition"]["limits"]["max_retries"]:
                data.update(next_wake_at=None, safe_error_code="assignment_retry_exhausted")
            elif data["next_wake_at"] is not None:
                backoff = min(
                    data["definition"]["limits"]["cadence_seconds"]
                    * 2 ** (data["consecutive_failures"] - 1),
                    3600,
                )
                data["next_wake_at"] = plain(
                    max(_time(data["next_wake_at"]), now + timedelta(seconds=backoff))
                )
            data["next_retry_at"] = data["next_wake_at"]
        elif not one_shot:
            data.update(consecutive_failures=0, next_retry_at=None)
        if completion.activity is not None:
            self._activity(
                transaction,
                data,
                completion.activity,
                critical=completion.phase
                in {
                    "failed",
                    "reconciliation",
                    "waiting_authorization",
                    "waiting_approval",
                    "budget_exhausted",
                },
            )
        for row in transaction.fetch_all(
            "SELECT id FROM persistent_assignment_action "
            "WHERE assignment_id=%s AND state='reserved'",
            (fence.assignment_id,),
        ):
            reserved = self._action(
                transaction, fence.owner_id, fence.assignment_id, str(row["id"])
            )
            attempt = reserved["attempts"][-1]
            self._release(data, attempt["maximum"])
            reserved["state"] = attempt["state"] = "failed_not_started"
            self._save_action(transaction, reserved)
        for task in data["tasks"]:
            if task["state"] == "running":
                task["state"] = (
                    "reconciliation" if completion.phase == "reconciliation" else "pending"
                )
                task["task_generation"] += 1
        self._clear_claim(data)
        if one_shot and not completion.completed:
            self._terminal_operation_failure(transaction, data)
        return self._save(transaction, data)

    def recover_expired_for_administration(self, transaction, *, limit=100):
        return self._recover_expired(transaction, limit=limit, profile="persistent")

    def recover_expired_operations_for_administration(self, transaction, *, limit=100):
        return self._recover_expired(transaction, limit=limit, profile="one_shot")

    def _recover_expired(self, transaction, *, limit, profile):
        _integer(limit, 1, 100)
        selection = (
            "SELECT id,owner_user_id FROM persistent_assignment "
            "WHERE execution_profile=%s AND (lease_expires_at<=clock_timestamp() "
            "OR (execution_profile='one_shot' AND lease_expires_at IS NULL "
            "AND lifecycle='active' AND data->>'phase'<>'reconciliation' "
            "AND (data->>'safe_error_code') IS DISTINCT FROM 'assignment_version_unsupported' "
            "AND CASE WHEN data->'operation'->'version' IN ('1'::jsonb,'2'::jsonb) "
            "AND COALESCE(data->'operation'->'control'->'version','1'::jsonb)='1'::jsonb "
            "AND COALESCE(data->'checkpoint'->'schema_version','1'::jsonb)='1'::jsonb "
            "THEN (data->'operation'->>'deadline_at')::timestamptz<=clock_timestamp() "
            "ELSE FALSE END)) "
            "AND (execution_profile='persistent' OR (data->'operation'->'version' "
            "IN ('1'::jsonb,'2'::jsonb) "
            "AND COALESCE(data->'operation'->'control'->'version','1'::jsonb)='1'::jsonb "
            "AND COALESCE(data->'checkpoint'->'schema_version','1'::jsonb)='1'::jsonb)) "
        )
        candidate_ids, owners, blocked = [], set(), set()
        while len(candidate_ids) < limit and len(owners) + len(blocked) < 100:
            candidates = transaction.fetch_all(
                selection + "AND NOT (owner_user_id=ANY(%s::text[])) "
                "AND NOT (id=ANY(%s::uuid[])) "
                "ORDER BY lease_expires_at NULLS FIRST,id LIMIT %s",
                (profile, sorted(blocked), candidate_ids, limit - len(candidate_ids)),
            )
            if not candidates:
                break
            for owner_id in sorted({row["owner_user_id"] for row in candidates} - owners):
                if len(owners) + len(blocked) >= 100:
                    break
                acquired = transaction.fetch_one(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,79)) AS acquired",
                    (owner_id,),
                )
                if acquired["acquired"] and self._lock_operation_owner(transaction, owner_id):
                    owners.add(owner_id)
                else:
                    blocked.add(owner_id)
            candidate_ids.extend(
                str(row["id"]) for row in candidates if row["owner_user_id"] in owners
            )
        if not owners:
            return AssignmentRecoveryResult()
        # Completion delivery takes owner locks before assignment rows.
        rows = transaction.fetch_all(
            selection + "AND id=ANY(%s::uuid[]) AND owner_user_id=ANY(%s::text[]) "
            "ORDER BY lease_expires_at NULLS FIRST,id LIMIT %s FOR UPDATE SKIP LOCKED",
            (profile, candidate_ids, sorted(owners), limit),
        )
        reclaimed, bindings, uncertain, terminal = [], [], [], []
        for row in rows:
            data = self._load(transaction, row["owner_user_id"], str(row["id"]), lock=True)
            unclaimed_deadline = profile == "one_shot" and data["lease_expires_at"] is None
            reclaimed.append(data["assignment_id"])
            if data["operation_binding"]:
                bindings.append(data["operation_binding"])
            pending = transaction.fetch_all(
                "SELECT id FROM persistent_assignment_action "
                "WHERE assignment_id=%s AND owner_user_id=%s "
                "AND (%s OR state IN ('reserved','started')) ORDER BY id FOR UPDATE",
                (data["assignment_id"], data["owner_id"], profile == "one_shot"),
            )
            held = False
            for item in pending:
                action = (self._known_action if profile == "one_shot" else self._action)(
                    transaction, data["owner_id"], data["assignment_id"], str(item["id"])
                )
                if action is None or action["state"] == "uncertain":
                    held = True
                    uncertain.append(str(item["id"]))
                    continue
                if action["state"] not in {"reserved", "started"}:
                    continue
                attempt = action["attempts"][-1]
                if action["state"] == "reserved":
                    self._release(data, attempt["maximum"])
                    action["state"] = attempt["state"] = "failed_not_started"
                elif action["intent"]["boundary"] == "read_only":
                    self._release(data, attempt["maximum"])
                    self._day(data, _now(transaction))
                    for key in (*_DIMENSIONS, "spend_micro_units"):
                        amount = attempt["maximum"].get(key)
                        if amount is not None:
                            for bucket in ("spent", "daily"):
                                data["usage"][bucket][key] = (
                                    data["usage"][bucket].get(key, 0) + amount
                                )
                    action["state"] = attempt["state"] = "failed"
                    attempt["outcome"] = {
                        "outcome": "failed",
                        "result_digest": digest([action["action_id"], "read_interrupted"]),
                        "result": {},
                        "evidence_reference": "read_only_lease_expired",
                        "actual": None,
                    }
                    action["result"] = attempt["outcome"]
                else:
                    action["state"] = attempt["state"] = "uncertain"
                    held = True
                    uncertain.append(action["action_id"])
                    action["result"] = {
                        "result_digest": digest([action["action_id"], "lease_expired"]),
                        "outcome": "uncertain",
                        "result": {},
                    }
                self._save_action(transaction, action)
            for task in data["tasks"]:
                if task["state"] == "running":
                    task["state"] = "reconciliation" if held else "pending"
                    task["task_generation"] += 1
            if not unclaimed_deadline:
                data["consecutive_failures"] += 1
            if data["lifecycle"] == "active":
                exhausted = (
                    data["consecutive_failures"] > data["definition"]["limits"]["max_retries"]
                )
                now = _now(transaction)
                if profile == "one_shot":
                    backoff = (5, 15, 45)[min(data["consecutive_failures"] - 1, 2)]
                else:
                    backoff = min(
                        data["definition"]["limits"]["cadence_seconds"]
                        * 2 ** min(data["consecutive_failures"] - 1, 10),
                        3600,
                    )
                data.update(
                    phase="reconciliation" if held else "failed",
                    safe_error_code="assignment_action_uncertain"
                    if held
                    else "assignment_interrupted",
                    next_wake_at=None
                    if held or exhausted
                    else plain(now + timedelta(seconds=backoff)),
                )
                if profile == "one_shot" and not held:
                    operation = self._operation_spec(data["operation"], data["owner_id"])
                    retry_at = now + timedelta(seconds=backoff)
                    if _time(operation.deadline_at) <= retry_at:
                        data.update(
                            safe_error_code="assignment_deadline_exceeded", next_wake_at=None
                        )
                    elif _time(operation.authority.expires_at) <= retry_at:
                        data.update(
                            phase="waiting_authorization",
                            safe_error_code="assignment_authorization_unavailable",
                            next_wake_at=None,
                        )
                    elif exhausted:
                        data.update(safe_error_code="assignment_retry_exhausted")
                if unclaimed_deadline:
                    data.update(
                        phase="reconciliation" if held else "failed",
                        safe_error_code="assignment_action_uncertain"
                        if held
                        else "assignment_deadline_exceeded",
                        next_wake_at=None,
                    )
                data["next_retry_at"] = data["next_wake_at"]
                if profile == "one_shot":
                    if not _executable(data):
                        data.update(
                            phase="reconciliation" if held else "waiting_authorization",
                            safe_error_code="assignment_version_unsupported",
                            next_wake_at=None,
                            next_retry_at=None,
                        )
                    self._terminal_operation_failure(transaction, data)
            self._clear_claim(data)
            saved = self._save(transaction, data)
            if profile == "one_shot" and saved.lifecycle == "completed":
                terminal.append(saved)
        return AssignmentRecoveryResult(
            tuple(reclaimed), tuple(bindings), tuple(uncertain), tuple(terminal)
        )

    def _activity(self, transaction, data, activity, *, critical=False):
        _text(activity.activity_key)
        _text(activity.activity_type, 64)
        _text(activity.title, 256)
        canonical(activity, 12000)
        if activity.notification_state not in {"none", "pending"}:
            raise RepositoryValidationError("invalid initial notification state")
        old = transaction.fetch_one(
            "SELECT data FROM persistent_assignment_activity "
            "WHERE assignment_id=%s AND activity_key=%s",
            (data["assignment_id"], activity.activity_key),
        )
        signature = digest(
            [
                activity.activity_type,
                activity.title,
                activity.summary,
                activity.references,
                activity.notification_state,
            ]
        )
        if old:
            value = plain(old["data"])
            if value.pop("signature") != signature:
                _conflict("assignment_idempotency_conflict")
            value["created_at"] = _time(value["created_at"])
            return AssignmentActivityRecord(**value)
        count = transaction.fetch_one(
            "SELECT count(*) AS n FROM persistent_assignment_activity WHERE assignment_id=%s",
            (data["assignment_id"],),
        )["n"]
        if count >= 1000:
            if critical:
                return None
            _conflict("assignment_history_capacity_exhausted")
        data["activity_sequence"] += 1
        value = plain(activity)
        value.update(
            activity_id=str(uuid.uuid4()),
            sequence=data["activity_sequence"],
            created_at=plain(_now(transaction)),
            signature=signature,
        )
        transaction.execute(
            "INSERT INTO persistent_assignment_activity "
            "(id,assignment_id,owner_user_id,activity_key,sequence,notification_state,data) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb)",
            (
                value["activity_id"],
                data["assignment_id"],
                data["owner_id"],
                value["activity_key"],
                value["sequence"],
                value["notification_state"],
                canonical(value),
            ),
        )
        value.pop("signature")
        value["created_at"] = _time(value["created_at"])
        return AssignmentActivityRecord(**value)

    def append_activity(self, transaction, *, fence, activity):
        data = self._fenced(transaction, fence)
        result = self._activity(transaction, data, activity)
        self._save(transaction, data)
        return result

    def list_activity(self, query, *, owner_id, assignment_id, after_sequence=0, limit=100):
        self._load(query, owner_id, assignment_id)
        _integer(after_sequence)
        _integer(limit, 1, 100)
        rows = query.fetch_all(
            "SELECT data,notification_state FROM persistent_assignment_activity "
            "WHERE assignment_id=%s AND owner_user_id=%s AND sequence>%s "
            "ORDER BY sequence LIMIT %s",
            (assignment_id, owner_id, after_sequence, limit),
        )
        results = []
        for row in rows:
            value = plain(row["data"])
            value.pop("signature")
            value["created_at"] = _time(value["created_at"])
            value["notification_state"] = row["notification_state"]
            results.append(AssignmentActivityRecord(**value))
        return tuple(results)

    def mark_activity_notified(
        self, transaction, *, owner_id, assignment_id, activity_id, expected_state="pending"
    ):
        self._load(transaction, owner_id, assignment_id)
        _uuid(activity_id)
        if expected_state != "pending":
            raise RepositoryValidationError("only pending notifications can be claimed")
        row = transaction.fetch_one(
            "UPDATE persistent_assignment_activity SET notification_state='notified' "
            "WHERE id=%s AND assignment_id=%s AND owner_user_id=%s "
            "AND notification_state='pending' RETURNING id",
            (activity_id, assignment_id, owner_id),
        )
        return row is not None

    def retain_for_administration(self, transaction, *, limit=100):
        _integer(limit, 1, 100)
        rows = transaction.fetch_all(
            "SELECT id FROM persistent_assignment_activity "
            "WHERE notification_state!='pending' "
            "AND data->'references'='{}'::jsonb "
            "AND (data->>'created_at')::timestamptz < clock_timestamp()-interval '30 days' "
            "ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED",
            (limit,),
        )
        for row in rows:
            transaction.execute(
                "DELETE FROM persistent_assignment_activity WHERE id=%s", (row["id"],)
            )
        return AssignmentRetentionResult(activity_removals=len(rows))

    def _purge_blockers(self, transaction, data):
        actions, unresolved = [], []
        for row in transaction.fetch_all(
            "SELECT id FROM persistent_assignment_action WHERE assignment_id=%s "
            "AND owner_user_id=%s ORDER BY id FOR UPDATE",
            (data["assignment_id"], data["owner_id"]),
        ):
            action_id = str(row["id"])
            action = self._known_action(
                transaction, data["owner_id"], data["assignment_id"], action_id
            )
            if action is not None:
                actions.append(action)
            if (
                action is None
                or action["state"] in {"reserved", "started", "uncertain"}
                or any(
                    attempt["state"] in {"reserved", "started", "uncertain"}
                    for attempt in action["attempts"]
                )
            ):
                unresolved.append(action_id)
        retained = (
            bool(unresolved)
            or any(data["usage"]["outstanding"].values())
            or any(task["state"] == "reconciliation" for task in data["tasks"])
        )
        return actions, unresolved, retained

    def _operation_continuation_held(self, transaction, data):
        actions, _, held = self._purge_blockers(transaction, data)
        return held or any(action["state"] in {"proposed", "approved"} for action in actions)

    def _assert_operation_action_continuable(self, transaction, fence, binding):
        data = self._load(transaction, fence.owner_id, fence.assignment_id)
        if data.get("execution_profile") != "one_shot":
            return
        actions, unresolved, _ = self._purge_blockers(transaction, data)
        known = {action["action_id"] for action in actions}
        if (
            data["phase"] == "reconciliation"
            or any(identity not in known for identity in unresolved)
            or any(task["state"] == "reconciliation" for task in data["tasks"])
        ):
            _conflict("assignment_action_uncertain")
        for action in actions:
            if action["state"] == "uncertain":
                _conflict("assignment_action_uncertain")
            for attempt in action["attempts"]:
                if attempt["state"] == "uncertain" or (
                    attempt["state"] == "started"
                    and (
                        canonical(attempt.get("assignment_fence")) != canonical(fence)
                        or canonical(attempt["binding"]) != canonical(binding)
                    )
                ):
                    _conflict("assignment_action_uncertain")

    def delete_for_owner(
        self,
        transaction,
        *,
        owner_id,
        assignment_id,
        expected_control_epoch,
        expected_state_version=None,
    ):
        data = self._load(
            transaction, owner_id, assignment_id, lock=True, required=False, allow_unknown=True
        )
        if data is None:
            return False
        if data.get("execution_profile") == "one_shot":
            _integer(expected_control_epoch, 1)
            _state_version(data, expected_state_version)
        if (
            data["lifecycle"] not in _TERMINAL
            or data["control_epoch"] != expected_control_epoch
            or data["lease_expires_at"] is not None
        ):
            _conflict("assignment_not_terminal")
        actions, _, retained = self._purge_blockers(transaction, data)
        if retained:
            _conflict("assignment_action_uncertain")
        for action in actions:
            if action.get("interactive_proposal_id"):
                self._expire_interactive_proposal(
                    transaction, owner_id, action["interactive_proposal_id"]
                )
        transaction.execute(
            "DELETE FROM persistent_assignment WHERE id=%s AND owner_user_id=%s",
            (assignment_id, owner_id),
        )
        return True

    def retire_owner(self, transaction, *, owner_id):
        self._lock_operation_owner(transaction, owner_id)
        if transaction.fetch_one(
            "SELECT id FROM persistent_assignment WHERE owner_user_id=%s "
            "AND execution_profile!='persistent' LIMIT 1",
            (owner_id,),
        ):
            _conflict("assignment_operation_required")
        result = self.retire_operations_for_owner(transaction, owner_id=owner_id)
        if result.retained_assignment_ids and not result.unresolved_action_ids:
            _conflict("assignment_action_uncertain")
        return result

    def retire_operations_for_owner(self, transaction, *, owner_id):
        _text(owner_id)
        transaction.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", (owner_id,))
        transaction.execute(
            "INSERT INTO astralplane_blob_owner_state "
            "(owner_id,state,version,retired_at,updated_at) "
            "VALUES(%s,'retired',1,clock_timestamp(),clock_timestamp()) "
            "ON CONFLICT(owner_id) DO UPDATE SET state='retired', "
            "version=astralplane_blob_owner_state.version+"
            "CASE WHEN astralplane_blob_owner_state.state='active' THEN 1 ELSE 0 END, "
            "retired_at=COALESCE(astralplane_blob_owner_state.retired_at,clock_timestamp()), "
            "updated_at=clock_timestamp()",
            (owner_id,),
        )
        rows = transaction.fetch_all(
            "SELECT id FROM persistent_assignment WHERE owner_user_id=%s ORDER BY id FOR UPDATE",
            (owner_id,),
        )
        stopped, deleted, unresolved, retained = [], [], [], []
        for row in rows:
            assignment_id = str(row["id"])
            data = self._load(transaction, owner_id, assignment_id, lock=True, allow_unknown=True)
            if data["lifecycle"] not in _TERMINAL:
                self.apply_control(
                    transaction,
                    owner_id=owner_id,
                    assignment_id=assignment_id,
                    expected_instruction_revision=data["instruction_revision"],
                    expected_control_epoch=data["control_epoch"],
                    expected_state_version=data["state_version"],
                    submission_id=str(uuid.uuid4()),
                    submission_digest=digest(["account_retirement", owner_id, assignment_id]),
                    control="stop",
                )
                data = self._load(
                    transaction, owner_id, assignment_id, lock=True, allow_unknown=True
                )
                stopped.append(assignment_id)
            actions, pending, held = self._purge_blockers(transaction, data)
            for action in actions:
                if action.get("interactive_proposal_id"):
                    self._expire_interactive_proposal(
                        transaction, owner_id, action["interactive_proposal_id"]
                    )
            if held:
                unresolved.extend(pending)
                retained.append(assignment_id)
            else:
                self.delete_for_owner(
                    transaction,
                    owner_id=owner_id,
                    assignment_id=assignment_id,
                    expected_control_epoch=data["control_epoch"],
                    expected_state_version=data["state_version"],
                )
                deleted.append(assignment_id)
        if not retained:
            transaction.execute(
                "DELETE FROM assignment_operation_receipt WHERE owner_id=%s",
                (owner_id,),
            )
        return AssignmentOwnerRetirementResult(
            tuple(stopped), tuple(deleted), tuple(unresolved), tuple(retained)
        )
