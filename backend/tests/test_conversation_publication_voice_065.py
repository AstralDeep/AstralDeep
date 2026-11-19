"""Tests for conversation publication merging (orchestrator/conversation_publication.py): deterministic
three-view merge and result scrubbing on the durable execution socket.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Iterator

import pytest

from orchestrator.async_tasks import DurableUserTurnWebSocket
from orchestrator.conversation_publication import (
    canonical_components_sha256,
    merge_conversation_publication,
)
from orchestrator.voice_coordinator import RecognitionStart
from orchestrator.voice_sessions import (
    CreateSession,
    TranscriptSubmission,
    VoiceSessionRepository,
)
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    OperationOwner,
    OperationRequest,
    OwnerScope,
    WorkAdmissionCoordinator,
)
from tests.helpers.voice_plane_runtime import (
    VoicePlaneTestRuntime,
    isolated_voice_plane_runtime,
    plane_work_admission_repository,
)
from shared.voice_transcript import (
    TranscriptProofBinding,
    issue_transcript_proof,
)


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def database() -> Iterator[VoicePlaneTestRuntime]:
    with isolated_voice_plane_runtime("voice_publication_065") as runtime:
        yield runtime


def _classes() -> tuple[AdmissionClassConfig, ...]:
    return (
        AdmissionClassConfig(
            class_name=AdmissionClass.GLOBAL,
            parent_class_name=None,
            active_limit=8,
            queue_limit=0,
            max_wait_ms=0,
            config_revision="voice-publication-065",
        ),
        AdmissionClassConfig(
            class_name=AdmissionClass.INTERACTIVE,
            parent_class_name=AdmissionClass.GLOBAL,
            active_limit=8,
            queue_limit=8,
            max_wait_ms=5_000,
            config_revision="voice-publication-065",
        ),
        AdmissionClassConfig(
            class_name=AdmissionClass.VOICE_INTERACTIVE,
            parent_class_name=AdmissionClass.INTERACTIVE,
            active_limit=4,
            queue_limit=0,
            max_wait_ms=0,
            config_revision="voice-publication-065",
        ),
    )


def _coordinator(database: VoicePlaneTestRuntime) -> WorkAdmissionCoordinator:
    return WorkAdmissionCoordinator(
        admission_classes=_classes(),
        repository=plane_work_admission_repository(database),
        operation_retention=timedelta(hours=24),
    )


def _create_active_session(
    repository: VoiceSessionRepository,
    database: VoicePlaneTestRuntime,
    *,
    user_id: str,
    chat_id: str,
):
    database.execute(
        "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
        "VALUES (?, ?, 'Concurrent voice', 1, 1)",
        (chat_id, user_id),
    )
    request = CreateSession(
        user_id=user_id,
        activation_id=str(uuid.uuid4()),
        device_id=str(uuid.uuid4()),
        device_kind="web",
        transport="livekit",
        room_name=f"room-{uuid.uuid4().hex}",
        participant_identity=f"client-{uuid.uuid4().hex}",
        visible_chat_id=chat_id,
        owner_connection_generation=str(uuid.uuid4()),
        control_binding_id=str(uuid.uuid4()),
        control_binding_expires_at=NOW + timedelta(minutes=10),
        lease_expires_at=NOW + timedelta(seconds=45),
        media_grant_nonce_hash=os.urandom(32),
        media_grant_issued_at=NOW,
        media_grant_expires_at=NOW + timedelta(minutes=5),
    )
    session = repository.create_session(request, now=NOW).session
    asyncio.run(
        repository.claim_control_lease(
            user_id=user_id,
            session_id=session.session_id,
            generation=session.generation,
            owner_id="publication-test",
            now=NOW,
        )
    )
    repository.assign_worker(
        user_id=user_id,
        session_id=session.session_id,
        expected_generation=session.generation,
        assignment_id=str(uuid.uuid4()),
        worker_identity=f"worker-{uuid.uuid4().hex}",
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        now=NOW,
    )
    repository.apply_chat_context(
        user_id=user_id,
        session_id=session.session_id,
        expected_generation=session.generation,
        expected_media_grant_revision=session.media_grant_revision,
        control_owner_id="publication-test",
        visible_chat_id=chat_id,
        chat_context_revision=session.chat_context_revision,
        now=NOW,
    )
    return repository.mark_session_active(
        user_id=user_id,
        session_id=session.session_id,
        expected_generation=session.generation,
        expected_media_grant_revision=session.media_grant_revision,
        now=NOW,
    )


def _admit_turn(
    repository: VoiceSessionRepository,
    session,
    *,
    text: str,
    now: datetime,
):
    start = RecognitionStart(
        session_id=session.session_id,
        generation=session.generation,
        assignment_id=session.worker_assignment_id,
        worker_identity=session.worker_identity,
        client_turn_id=str(uuid.uuid4()),
        media_grant_revision=session.media_grant_revision,
        chat_id=session.visible_chat_id,
        chat_context_revision=session.chat_context_revision,
    )
    bound = asyncio.run(
        repository.bind_worker_recognition(
            start=start,
            control_owner_id="publication-test",
            now=now,
        )
    ).turn
    binding = TranscriptProofBinding(
        session_id=session.session_id,
        generation=session.generation,
        media_grant_revision=session.media_grant_revision,
        assignment_id=session.worker_assignment_id,
        worker_identity=session.worker_identity,
        turn_id=bound.turn_id,
        client_turn_id=bound.client_turn_id,
        submission_id=bound.submission_id,
        request_generation=bound.request_generation,
        chat_id=bound.chat_id,
        chat_context_revision=bound.chat_context_revision,
        detected_language="en",
    )
    proof = issue_transcript_proof(b"p" * 32, binding, text, now=now)
    return repository.admit_transcript(
        TranscriptSubmission(
            user_id=session.user_id,
            session_id=session.session_id,
            generation=session.generation,
            media_grant_revision=session.media_grant_revision,
            turn_id=bound.turn_id,
            client_turn_id=bound.client_turn_id,
            submission_id=bound.submission_id,
            request_generation=bound.request_generation,
            chat_id=bound.chat_id,
            chat_context_revision=bound.chat_context_revision,
            source_participant_identity=session.worker_identity,
            detected_language="en",
            text=proof.canonical_text,
            text_digest_sha256=proof.text_digest_sha256,
            transcript_proof=proof.transcript_proof,
            proof_expires_at=proof.proof_expires_at,
        ),
        worker_control_secret=b"p" * 32,
        now=now + timedelta(milliseconds=100),
    )


def _claim(
    coordinator: WorkAdmissionCoordinator,
    *,
    user_id: str,
    chat_id: str,
    request_generation: str,
):
    owner = OperationOwner(OwnerScope.USER, user_id, None)
    submission_id = uuid.uuid4()
    connection_generation = uuid.uuid4()
    result = coordinator.submit(
        OperationRequest(
            operation_kind="voice_chat_message",
            admission_class=AdmissionClass.VOICE_INTERACTIVE,
            owner=owner,
            submission_id=submission_id,
            idempotency_namespace="voice_chat_message",
            idempotency_key=str(submission_id),
            normalized_input_digest="ab" * 32,
            chat_id=chat_id,
            parent_operation_id=None,
            connection_generation=connection_generation,
            request_generation=uuid.UUID(request_generation),
        )
    )
    assert result.accepted
    claim = coordinator.claim_operation(
        AdmissionClass.VOICE_INTERACTIVE, result.operation_id
    )
    assert claim is not None
    return owner, connection_generation, claim


def _component(component_id: str, content: str) -> dict[str, str]:
    return {
        "type": "text",
        "component_id": component_id,
        "content": content,
    }


def test_merge_is_deterministic_and_invalid_candidate_layout_falls_back() -> None:
    base = [_component("shared", "base")]
    candidate = [_component("shared", "candidate")]
    latest = [_component("shared", "latest"), _component("other", "new")]
    base_layout = [{
        "layout_key": "main",
        "position": 0,
        "layout": [{"type": "ref", "component_id": "shared"}],
    }]
    invalid_candidate_layout = [{
        "layout_key": "main",
        "position": 0,
        "layout": [{"type": "ref", "component_id": "missing"}],
    }]

    merged = merge_conversation_publication(
        base_components=base,
        candidate_components=candidate,
        latest_components=latest,
        base_layouts=base_layout,
        candidate_layouts=invalid_candidate_layout,
        latest_layouts=base_layout,
    )

    assert list(merged.components) == latest
    assert list(merged.layouts) == base_layout
    assert merged.component_conflicts == ("shared",)
    assert merged.layout_conflicts == ("main",)
    assert canonical_components_sha256(base) == canonical_components_sha256(
        [{"content": "base", "component_id": "shared", "type": "text"}]
    )

    flat = merge_conversation_publication(
        base_components=base,
        candidate_components=[],
        latest_components=base,
        base_layouts=base_layout,
        candidate_layouts=invalid_candidate_layout,
        latest_layouts=[],
    )
    assert flat.components == ()
    assert flat.layouts == ()
    assert flat.layout_conflicts == ("main",)


def test_three_view_merge_rebases_non_conflicting_additions_and_deletions() -> None:
    base_components = [
        _component("keep", "base"),
        _component("delete", "base"),
    ]
    candidate_components = [
        _component("keep", "base"),
        _component("candidate-only", "candidate"),
    ]
    latest_components = [
        _component("keep", "latest"),
        _component("delete", "base"),
        _component("latest-only", "latest"),
    ]
    base_layouts = [
        {
            "layout_key": "delete-layout",
            "position": 0,
            "layout": [{"type": "ref", "component_id": "delete"}],
        }
    ]
    candidate_layouts = [
        {
            "layout_key": "candidate-layout",
            "position": 1,
            "layout": [
                {"type": "ref", "component_id": "candidate-only"}
            ],
        }
    ]

    merged = merge_conversation_publication(
        base_components=base_components,
        candidate_components=candidate_components,
        latest_components=latest_components,
        base_layouts=base_layouts,
        candidate_layouts=candidate_layouts,
        latest_layouts=base_layouts,
    )

    assert list(merged.components) == [
        _component("keep", "latest"),
        _component("latest-only", "latest"),
        _component("candidate-only", "candidate"),
    ]
    assert list(merged.layouts) == candidate_layouts
    assert merged.component_conflicts == ()
    assert merged.layout_conflicts == ()


@pytest.mark.asyncio
async def test_durable_execution_socket_forwards_only_rejection_and_scrubs() -> None:
    class Origin:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def send_text(self, data: str) -> None:
            self.sent.append(json.loads(data))

    origin = Origin()
    socket = DurableUserTurnWebSocket(origin, user_id="owner-065")
    assert socket.llm_context_user_id == "owner-065"
    await socket.send_json({"type": "chat_status", "message": "secret"})
    await socket.send_json({"type": "voice_submission_rejected", "reason": "x"})
    assert origin.sent == [
        {"type": "voice_submission_rejected", "reason": "x"}
    ]
    socket.scrub()
    assert socket.llm_context_user_id is None
    assert "owner-065" not in repr(socket)
    await socket.send_json({"type": "voice_submission_rejected"})
    assert len(origin.sent) == 1


