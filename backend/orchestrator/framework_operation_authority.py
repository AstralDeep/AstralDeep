"""Refreshes source-less framework chat authority from the operation's exact credential.
Keeps its machine provenance separate from interactive Keycloak session authority.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field

from astralplane.repositories.assignment_models import AssignmentRecord
from astralplane.repositories.history import FrameworkCredentialObservation
from orchestrator.session_authority import (
    OperationExecutionAuthority,
    SessionAuthorityUnavailable,
    refresh_operation_execution_authority,
)


def framework_chat_record(record):
    from persistent_agents.research_input import chat_definition
    from shared.feature_flags import flags

    if not (flags.is_enabled("framework_credentials") and flags.is_enabled("persistent_agents")):
        raise SessionAuthorityUnavailable("framework_operation_authority_unavailable")
    if type(record) is not AssignmentRecord or not isinstance(record.operation, Mapping):
        raise SessionAuthorityUnavailable("framework_operation_authority_unavailable")
    operation = record.operation
    selected = operation.get("authority", {})
    if (type(operation.get("version")) is not int or operation["version"] != 2
            or selected.get("origin") != "framework"
            or selected.get("reference_kind") != "credential"
            or selected.get("owner_id") != record.owner_id
            or operation.get("selection") is not None):
        raise SessionAuthorityUnavailable("framework_operation_authority_unavailable")
    chat_definition(record)


@dataclass(frozen=True, slots=True)
class FrameworkOperationExecutionAuthority:
    record: AssignmentRecord = field(repr=False)
    observation: FrameworkCredentialObservation = field(repr=False)
    plane_runtime: object = field(repr=False)

    @property
    def claims(self):
        return {"sub": self.record.owner_id, "machine_class": "framework",
                "_framework_credential_id": self.observation.credential.credential_id}


def authority_repository(runtime, authority):
    if (not isinstance(authority, (OperationExecutionAuthority, FrameworkOperationExecutionAuthority))
            or authority.plane_runtime is not runtime):
        raise SessionAuthorityUnavailable("operation_authority_unavailable")
    if type(authority) is FrameworkOperationExecutionAuthority:
        framework_chat_record(authority.record)
        selected = authority.record.operation["authority"]
        observation = authority.observation
        if (type(observation) is not FrameworkCredentialObservation
                or observation.credential.owner_id != authority.record.owner_id
                or observation.credential.credential_id != selected["reference_id"]
                or observation.credential.scopes != ("operations.submit",)):
            raise SessionAuthorityUnavailable("framework_operation_authority_unavailable")
        return runtime.repositories.framework_credentials
    if isinstance(authority, OperationExecutionAuthority):
        return runtime.repositories.history.sessions
    raise SessionAuthorityUnavailable("operation_authority_unavailable")


def assert_operation_owner(service, authority, record):
    if authority.plane_runtime is not service.store.plane_runtime:
        raise SessionAuthorityUnavailable("operation_authority_unavailable")
    authority_repository(service.store.plane_runtime, authority)
    service._owner(record.owner_id, authority.claims,
                   human=type(authority) is not FrameworkOperationExecutionAuthority)


def invocation_principal(authority):
    principal = authority.claims
    if type(authority) is FrameworkOperationExecutionAuthority:
        principal["_invocation_channel"] = "framework"
    elif isinstance(authority, OperationExecutionAuthority):
        principal.update(_raw_token=authority.subject_token, _invocation_channel="background")
    else:
        raise SessionAuthorityUnavailable("operation_authority_unavailable")
    return principal


async def refresh_assignment_execution_authority(record, *, sessions, plane_runtime):
    if record.operation.get("authority", {}).get("origin") != "framework":
        return await refresh_operation_execution_authority(owner_id=record.owner_id,
            assignment_id=record.assignment_id, sessions=sessions, plane_runtime=plane_runtime)
    try:
        framework_chat_record(record)
        async with asyncio.timeout(15):
            def capture():
                with plane_runtime.transaction() as tx:
                    plane_runtime.repositories.history.sessions.bound_request_execution_waits(tx)
                    repository = plane_runtime.repositories.assignments
                    current = repository.get_operation(tx, owner_id=record.owner_id,
                        assignment_id=record.assignment_id).assignment
                    if (current.definition != record.definition or current.operation != record.operation
                            or current.instruction_revision != record.instruction_revision
                            or current.control_epoch != record.control_epoch):
                        raise SessionAuthorityUnavailable("framework_operation_authority_unavailable")
                    observation = repository.capture_framework_operation_execution_observation(tx,
                        owner_id=current.owner_id, assignment_id=current.assignment_id,
                        expected_record=current)
                    return FrameworkOperationExecutionAuthority(current, observation, plane_runtime)
            return await asyncio.to_thread(capture)
    except Exception:
        raise SessionAuthorityUnavailable("framework_operation_authority_unavailable") from None
