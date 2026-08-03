"""Feature-066 regression pins for ``WorkAdmission.bind_chat``.

The first message of a new chat is admitted BEFORE its conversation exists
(``chat_id=None`` at ingress); the turn then creates the chat. ``bind_chat``
performs that single legitimate None→chat adoption durably so every
downstream publication fence keeps strict identity semantics. These tests pin
the contract: adopt-on-None, idempotent re-bind, cross-conversation refusal,
and fence checking. (Regression: before 066 every first-message-of-a-new-chat
failed ``conversation operation chat identity changed`` at publication.)
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    InMemoryWorkAdmissionRepository,
    OperationOwner,
    OperationRequest,
    OperationState,
    OwnerScope,
    StaleExecutionFenceError,
    WorkAdmissionCoordinator,
)


@dataclass
class _FakeClock:
    current: datetime = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def _owner(user_id: str = "owner-a") -> OperationOwner:
    return OperationOwner(
        owner_scope=OwnerScope.USER,
        owner_user_id=user_id,
        connection_scope_id=None,
    )


def _request(label: str, *, chat_id: str | None) -> OperationRequest:
    submission_id = uuid.uuid4()
    return OperationRequest(
        operation_kind="connection_frame",
        admission_class=AdmissionClass.INTERACTIVE,
        owner=_owner(),
        submission_id=submission_id,
        idempotency_namespace="ui_submission",
        idempotency_key=str(submission_id),
        normalized_input_digest=hashlib.sha256(label.encode("utf-8")).hexdigest(),
        chat_id=chat_id,
        parent_operation_id=None,
        connection_generation=uuid.uuid4(),
        request_generation=uuid.uuid4(),
    )


def _coordinator(clock: _FakeClock) -> WorkAdmissionCoordinator:
    return WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.INTERACTIVE,
                parent_class_name=None,
                active_limit=1,
                queue_limit=2,
                max_wait_ms=5_000,
                config_revision="test-066",
            ),
        ),
        repository=InMemoryWorkAdmissionRepository(),
        clock=clock,
        operation_retention=timedelta(hours=24),
    )


def _claimed(coordinator, request):
    accepted = coordinator.submit(request)
    assert accepted.accepted is True
    claim = coordinator.claim_operation(
        AdmissionClass.INTERACTIVE, accepted.operation_id
    )
    assert claim is not None
    return accepted, claim


def test_bind_chat_adopts_the_created_conversation() -> None:
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    request = _request("first-message", chat_id=None)
    accepted, claim = _claimed(coordinator, request)

    updated = coordinator.bind_chat(claim.fence, "5e0f7c3a-chat-created")

    assert updated.chat_id == "5e0f7c3a-chat-created"
    assert updated.state is OperationState.RUNNING
    projection = coordinator.query_operation(
        owner=request.owner, operation_id=accepted.operation_id
    )
    assert projection.chat_id == "5e0f7c3a-chat-created"


def test_bind_chat_is_idempotent_for_the_same_chat() -> None:
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    _, claim = _claimed(coordinator, _request("first", chat_id=None))

    first = coordinator.bind_chat(claim.fence, "chat-a")
    second = coordinator.bind_chat(claim.fence, "chat-a")

    assert first.chat_id == "chat-a"
    assert second.chat_id == "chat-a"
    # The no-op re-bind must not spin the record's revision forward.
    assert second.state_revision == first.state_revision


def test_bind_chat_refuses_a_cross_conversation_rebind() -> None:
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    _, claim = _claimed(coordinator, _request("first", chat_id=None))
    coordinator.bind_chat(claim.fence, "chat-a")

    with pytest.raises(ValueError):
        coordinator.bind_chat(claim.fence, "chat-b")


def test_bind_chat_refuses_when_admitted_with_a_conversation() -> None:
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    _, claim = _claimed(
        coordinator, _request("scoped", chat_id="chat-original")
    )

    with pytest.raises(ValueError):
        coordinator.bind_chat(claim.fence, "chat-other")
    # Same-chat "re-bind" of an already-scoped operation is a no-op success.
    unchanged = coordinator.bind_chat(claim.fence, "chat-original")
    assert unchanged.chat_id == "chat-original"


def test_bind_chat_requires_a_current_execution_fence() -> None:
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    _, claim = _claimed(coordinator, _request("first", chat_id=None))
    coordinator.terminalize(
        claim.fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Completed",
        retry_after_ms=None,
    )

    with pytest.raises(StaleExecutionFenceError):
        coordinator.bind_chat(claim.fence, "chat-late")
