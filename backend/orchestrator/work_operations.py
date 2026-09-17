"""Caller-class-aware Work facade for the framework ingress (feature 088 T049).

Interactive humans reach Work through ``work_api.py``'s cookie/bearer-fenced
REST router. A ``FrameworkCaller`` (an owner-issued, independently-lifetimed
credential resolved by ``orchestrator.framework_credentials``) reaches the
SAME underlying Plane state through THIS module instead — never through the
interactive router's ``AuthenticatedWorkRequest``/``WorkCallerAuthority``
machinery, which is fenced to a live cookie session and cannot be safely
reused for a bearer with no session at all.

Scope, deliberately narrow for a first, safely-reviewable slice:

* **Reads** (``get``/``list``/``poll``/``result``) go straight through the
  existing ``orchestrator.work_service.WorkService`` — it already accepts any
  ``claims`` dict naming the owner and refuses only human-turn markers
  (``act``/``machine_class``/...) that a framework caller never carries, so
  no new read path is needed.
* **Submit** admits ``kind="chat"`` only (no source, no tool access, no PHI
  risk beyond the owner's own free text — mirroring
  ``WorkSubmitService._chat_definition``'s restrictions) directly against
  ``astralplane.repositories.assignments.AssignmentRepository.create_operation``
  with ``origin="framework"``, atomically consuming one credential admission
  in the SAME transaction as the create (never on an idempotent replay).
* **Cancel/pause** call ``apply_control`` directly, re-verifying the
  credential's current execution authority (Plane's
  ``FrameworkCredentialRepository.assert_current_execution``) immediately
  before the mutation, inside the same transaction — the same discipline
  ``WorkCallerAuthority.assert_current`` applies for interactive callers, but
  ``WorkControlService`` itself cannot be reused (it hard-requires a
  ``WorkCallerAuthority`` instance).
* **Resume/wait/wake** are NOT wired for framework callers yet — they refuse
  with ``framework_control_unavailable`` (501) rather than attempting an
  unreviewed reimplementation of ``OperationControlAuthority``'s original/
  current-session rebinding. This is an honest, documented gap (see the
  workstream report), not a silent no-op.
* **decide/reconcile/delete** always refuse ``assignment_human_required``
  (403) BEFORE any read or mutation — a framework credential can never review
  a proposed action, resolve an uncertain outcome, or delete retained work.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Optional

from astralplane.repositories.assignment_models import (
    AssignmentDefinition,
    AssignmentOperationAuthority,
    AssignmentOperationSpec,
    AssignmentRecord,
)

from orchestrator.framework_credentials import FrameworkCaller
from orchestrator.work_service import WorkService, _identity, _public
from orchestrator.work_submit import _invalid, _limits, _text
from persistent_agents.models import AssignmentError, digest, validate_id

_ORIGIN_NAMESPACE = "framework.work.v1"
_DEFAULT_LIMITS = {
    "model_calls": 200, "tool_calls": 1, "tokens": 2_000_000,
    "elapsed_ms": 3_600_000, "max_retries": 1,
}
_DEFAULT_DEADLINE_SECONDS = 3600
_MAX_DEADLINE = timedelta(days=1)


def _now() -> datetime:
    return datetime.now(UTC)


def _framework_claims(owner_id: str) -> dict:
    """A minimal, honest claims dict: names the owner, carries no human markers.

    ``WorkService``/``AssignmentService._owner`` accept any dict naming the
    correct owner and refuse only explicit machine/delegation markers this
    dict never sets — this is not a JWT and is never presented as one.
    """
    return {"sub": owner_id}


def _require_scope(caller: FrameworkCaller, scope: str) -> None:
    if not isinstance(caller, FrameworkCaller) or not caller.has_scope(scope):
        raise AssignmentError("framework_scope_required", 403)


def _submission_id(value: object) -> str:
    try:
        return validate_id(value)
    except (ValueError, TypeError, AttributeError):
        raise AssignmentError("work_control_invalid", 422) from None


def _revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2**63 - 1:
        raise AssignmentError("work_control_invalid", 422)
    return value


class FrameworkWorkOperations:
    """Bind one ``FrameworkCredentialService``/assignments coordinator pair."""

    def __init__(self, *, assignments, credentials):
        self.assignments = assignments
        self.store = assignments.store
        self.credentials = credentials

    # -- reads --------------------------------------------------------------

    async def get(self, caller: FrameworkCaller, operation_id: str) -> dict:
        _require_scope(caller, "operations.read")
        service = WorkService(self.assignments)
        return await service.get(caller.owner_id, _framework_claims(caller.owner_id), operation_id)

    async def list(self, caller: FrameworkCaller, *, limit: int = 50, after_id: Optional[str] = None) -> dict:
        _require_scope(caller, "operations.read")
        service = WorkService(self.assignments)
        return await service.list(caller.owner_id, _framework_claims(caller.owner_id),
                                  limit=limit, after_id=after_id)

    async def poll(self, caller: FrameworkCaller, operation_id: str, *, after_revision=None) -> dict:
        _require_scope(caller, "operations.read")
        service = WorkService(self.assignments)
        return await service.poll(caller.owner_id, _framework_claims(caller.owner_id), operation_id,
                                  after_revision=after_revision)

    async def result(self, caller: FrameworkCaller, operation_id: str) -> dict:
        _require_scope(caller, "artifacts.read")
        service = WorkService(self.assignments)
        return await service.result(caller.owner_id, _framework_claims(caller.owner_id), operation_id)

    # -- submit (chat only) ---------------------------------------------------

    async def submit(
        self,
        caller: FrameworkCaller,
        *,
        idempotency_key: str,
        name: str,
        instructions: str,
        conversation_id: Optional[str] = None,
        limits: Optional[dict] = None,
        deadline_in_seconds: Optional[int] = None,
    ) -> dict:
        """Submit one chat-kind one-shot operation; a same-key replay is a no-op read."""
        _require_scope(caller, "operations.submit")
        caller_key = _text(idempotency_key, 256)
        owner_id = caller.owner_id
        try:
            bounded_name, bounded_instructions = _text(name, 120), _text(instructions, 4096)
            conversation = _text(conversation_id, 128, optional=True)
        except (ValueError, TypeError, AttributeError):
            _invalid()
        limit_map = _limits(limits) if limits is not None else _limits(_DEFAULT_LIMITS)
        deadline_seconds = deadline_in_seconds if deadline_in_seconds is not None else _DEFAULT_DEADLINE_SECONDS
        if not isinstance(deadline_seconds, int) or not 1 <= deadline_seconds <= int(_MAX_DEADLINE.total_seconds()):
            _invalid()

        # PHI screen on the owner's own free text (parity with the interactive
        # chat-kind path; no source, no tools, so nothing else is in scope).
        from persistent_agents.privacy import content_text, privacy_text
        protected = privacy_text(content_text({"name": bounded_name, "instructions": bounded_instructions}), ())
        if await asyncio.to_thread(self.assignments.phi_gate.contains_phi, protected):
            raise AssignmentError("assignment_sensitive_content_refused", 422)
        if conversation is not None:
            owned = await asyncio.to_thread(self.assignments.orch.history.get_chat, conversation,
                                            user_id=owner_id)
            if owned is None:
                raise AssignmentError("assignment_destination_not_found", 404)

        definition = AssignmentDefinition(
            name=bounded_name, instructions=bounded_instructions, source={}, allowed_tools=(),
            consented_scopes=(), offline_grant_id=None, limits=limit_map,
            completion_condition=None, conversation_id=conversation, cost_quote_coverage=None,
        )
        identity = str(uuid.uuid4())
        signature = digest({"api_version": 1, "namespace": _ORIGIN_NAMESPACE, "caller_key": caller_key,
                            "name": bounded_name, "instructions": bounded_instructions,
                            "conversation_id": conversation, "limits": limit_map})

        # Captured OUTSIDE the write transaction below (its own short read),
        # exactly like the interactive path captures ``authority`` before
        # opening its accept transaction — Plane re-verifies this SAME
        # observation fresh, under lock, inside that one write transaction via
        # ``create_operation``'s own execution-authority guard.
        observation = self.credentials.fresh_observation(caller)
        if observation is None:
            raise AssignmentError("framework_credential_authority_unavailable", 409)
        deadline = min(
            _now() + timedelta(seconds=deadline_seconds),
            datetime.fromtimestamp(observation.credential.expires_at, UTC),
        )
        operation = AssignmentOperationSpec(
            "chat",
            AssignmentOperationAuthority(owner_id, "framework", "credential",
                                         caller.credential_id, deadline),
            deadline, "operation",
        )

        def _submit(transaction, repository):
            replay = repository.get_operation_receipt(
                transaction, owner_id=owner_id, origin_namespace=_ORIGIN_NAMESPACE,
                caller_key=caller_key, command_digest=signature, credential_id=caller.credential_id,
            )
            if replay is not None:
                read = repository.get_operation(transaction, owner_id=owner_id,
                                                assignment_id=replay.assignment_id)
                return read, False
            # Verify authority (not revoked/expired/hash-mismatched) BEFORE
            # charging anything, so a revoked or expired credential is reported
            # as such rather than as an ambiguous allowance exhaustion — a
            # replay above never reaches this line, so a replay never re-pays
            # for or re-verifies a mint that already committed.
            self.credentials.assert_execution(transaction, observation)
            # A replay is a pure read: the allowance is charged only for a
            # genuinely new operation, in the SAME transaction that creates it.
            self.credentials.consume_admission(
                transaction, owner_id=owner_id, credential_id=caller.credential_id,
            )
            record = repository.create_operation(
                transaction, owner_id=owner_id, assignment_id=identity,
                origin_namespace=_ORIGIN_NAMESPACE, caller_key=caller_key, command_digest=signature,
                definition=definition, operation=operation, credential_id=caller.credential_id,
                authority=observation,
            )
            if (not isinstance(record, AssignmentRecord) or record.assignment_id != identity
                    or record.owner_id != owner_id or record.execution_profile != "one_shot"):
                raise AssignmentError("work_repository_unavailable", 503)
            # ``create_operation`` returns the bare ``AssignmentRecord``; re-read
            # it as the SAME ``AssignmentOperationRead`` shape ``_public``/
            # ``WorkService`` expect everywhere else (disposition included).
            read = repository.get_operation(transaction, owner_id=owner_id, assignment_id=identity)
            if read is None:
                raise AssignmentError("work_repository_unavailable", 503)
            return read, True

        # ``AssignmentStore.transaction`` already converts a Plane
        # ``RepositoryConflictError``/``RepositoryValidationError`` into an
        # ``AssignmentError`` carrying the repository's own code — nothing
        # further to translate here.
        read, created = await self.store.transaction(_submit, bound_session_waits=True)
        return {**_public(read, owner_id), "created": created}

    # -- control (cancel/pause; resume/wait/wake are not yet wired) ----------

    async def cancel(self, caller: FrameworkCaller, operation_id: str, *,
                     submission_id: str, expected_revision: int) -> dict:
        return await self._control(caller, "cancel", operation_id,
                                   submission_id=submission_id, expected_revision=expected_revision)

    async def pause(self, caller: FrameworkCaller, operation_id: str, *,
                    submission_id: str, expected_revision: int) -> dict:
        return await self._control(caller, "pause", operation_id,
                                   submission_id=submission_id, expected_revision=expected_revision)

    async def resume(self, caller: FrameworkCaller, operation_id: str, **_kwargs) -> dict:
        raise AssignmentError("framework_control_unavailable", 501)

    async def wait(self, caller: FrameworkCaller, operation_id: str, **_kwargs) -> dict:
        raise AssignmentError("framework_control_unavailable", 501)

    async def wake(self, caller: FrameworkCaller, operation_id: str, **_kwargs) -> dict:
        raise AssignmentError("framework_control_unavailable", 501)

    async def _control(self, caller: FrameworkCaller, command: str, operation_id: str, *,
                       submission_id: str, expected_revision: int) -> dict:
        from orchestrator.work_controls import _method, _owned_operation

        _require_scope(caller, "operations.control")
        owner_id = caller.owner_id
        identity = _identity(operation_id)
        sid = _submission_id(submission_id)
        revision = _revision(expected_revision)
        plane_command = "stop" if command == "cancel" else command
        signature = digest({"api_version": 1, "namespace": _ORIGIN_NAMESPACE, "operation_id": identity,
                            "command": command, "submission_id": sid})
        # Captured OUTSIDE the write transaction below, for the same reason
        # ``submit`` captures it early: re-verification of THIS SAME
        # observation happens under lock, inside the one write transaction.
        observation = self.credentials.fresh_observation(caller)
        if observation is None:
            raise AssignmentError("framework_credential_authority_unavailable", 409)

        def _apply(transaction, repository):
            read = _owned_operation(transaction, repository, owner_id, identity)
            receipt = _method(repository, "get_submission_receipt")(
                transaction, owner_id=owner_id, assignment_id=identity,
                submission_id=sid, submission_digest=signature, command=plane_command,
            )
            if receipt is not None:
                updated = _owned_operation(transaction, repository, owner_id, identity)
                return _public(updated, owner_id), False
            self.credentials.assert_execution(transaction, observation)
            current = read.assignment
            result = _method(repository, "apply_control")(
                transaction, owner_id=owner_id, assignment_id=identity,
                expected_instruction_revision=current.instruction_revision,
                expected_control_epoch=current.control_epoch,
                expected_state_version=revision, submission_id=sid, submission_digest=signature,
                control=plane_command,
            )
            updated = _owned_operation(transaction, repository, owner_id, identity)
            return _public(updated, owner_id), result.applied

        public, applied = await self.store.transaction(_apply, bound_session_waits=True)
        return {"operation": public, "applied": applied}

    # -- human-only commands: refused before any read or mutation ------------

    async def decide(self, caller: FrameworkCaller, *_args, **_kwargs) -> dict:
        raise AssignmentError("assignment_human_required", 403)

    async def reconcile(self, caller: FrameworkCaller, *_args, **_kwargs) -> dict:
        raise AssignmentError("assignment_human_required", 403)

    async def delete(self, caller: FrameworkCaller, *_args, **_kwargs) -> dict:
        raise AssignmentError("assignment_human_required", 403)


#: Tool names this facade actually implements end-to-end — the ONLY names
#: ``orchestrator.mcp_projection``/``a2a_orchestrator_executor`` may advertise
#: (Constitution: never expose an unimplemented action). ``resume``/``wait``/
#: ``wake``/artifact- and agent-listing are deliberately absent: their methods
#: above exist but always refuse (or do not exist at all), so they are never
#: projected as available tools.
DISPATCHABLE_TOOL_NAMES = (
    "astral_submit_operation", "astral_get_operation", "astral_list_operations",
    "astral_get_operation_events", "astral_cancel_operation", "astral_pause_operation",
    "astral_get_artifact",
)


def dispatch_name(tool_name: str) -> Optional[str]:
    """Map an ``astral_*`` MCP/A2A tool name to a ``FrameworkWorkOperations`` method."""
    return {
        "astral_submit_operation": "submit",
        "astral_get_operation": "get",
        "astral_list_operations": "list",
        "astral_get_operation_events": "poll",
        "astral_cancel_operation": "cancel",
        "astral_pause_operation": "pause",
        "astral_get_artifact": "result",
    }.get(tool_name) if tool_name in DISPATCHABLE_TOOL_NAMES else None


__all__ = ["DISPATCHABLE_TOOL_NAMES", "FrameworkWorkOperations", "dispatch_name"]
