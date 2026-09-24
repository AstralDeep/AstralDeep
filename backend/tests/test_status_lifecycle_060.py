"""Tests for canonical operation and agent-lifecycle state building
(orchestrator/agent_lifecycle.py, chrome_events.py, shared/protocol.py): exact flag
projections, revision sequencing, and owner-scoped publication.
"""

from __future__ import annotations

from datetime import datetime
import json

import pytest

from orchestrator.agent_lifecycle import (
    AGENT_LIFECYCLE_LABELS,
    canonical_agent_lifecycle,
    publish_agent_lifecycle,
)
from orchestrator.chrome_events import (
    canonical_operation_status,
    emit_operation_status,
)
from shared.protocol import ProtocolValidationError


OPERATION_ID = "3558c68b-a02e-4529-9cf8-5ba95bcc7951"
CONNECTION_GENERATION = "dbe2670f-04ce-40c8-ab08-615500571f90"
REQUEST_GENERATION = "b1876d0c-7401-47fa-8c78-8cdedba692a8"
REVISION_ID = "2e9bca16-898b-4f51-8549-eaa81d97dc23"
RUNTIME_ID = "91a03450-f0fc-4c32-a61c-085e7779d74a"


class _Socket:
    pass


class _Orchestrator:
    def __init__(self) -> None:
        self.owner_first = _Socket()
        self.owner_second = _Socket()
        self.other = _Socket()
        self.ui_clients = {
            self.owner_first,
            self.owner_second,
            self.other,
        }
        self.ui_sessions = {
            self.owner_first: {"sub": "owner"},
            self.owner_second: {"sub": "owner"},
            self.other: {"sub": "other"},
        }
        self.sent: list[tuple[_Socket, dict]] = []

    async def _safe_send(self, websocket, payload: str) -> bool:
        self.sent.append((websocket, json.loads(payload)))
        return True


def _operation(state: str, sequence: int):
    terminal = state in {"completed", "failed", "cancelled", "retryable"}
    error = None
    if state in {"failed", "cancelled", "retryable"}:
        error = {
            "code": "operation_failed" if state != "cancelled" else "cancelled_by_user",
            "message": "Safe terminal message",
        }
    return canonical_operation_status(
        operation_id=OPERATION_ID,
        action="curated_example",
        surface="chat",
        chat_id=None,
        connection_generation=CONNECTION_GENERATION,
        request_generation=REQUEST_GENERATION,
        sequence=sequence,
        state=state,
        phase=state,
        label=state.title(),
        terminal=terminal,
        retryable=state == "retryable",
        error=error,
        retry_after_ms=500 if state == "retryable" else None,
        updated_at=datetime(2026, 7, 16, 12, 0, sequence),
    )


@pytest.mark.parametrize(
    ("state", "terminal", "retryable"),
    [
        ("accepted", False, False),
        ("validating", False, False),
        ("persisting", False, False),
        ("running", False, False),
        ("completed", True, False),
        ("failed", True, False),
        ("cancelled", True, False),
        ("retryable", True, True),
    ],
)
def test_operation_builder_emits_the_exact_state_flags(
    state: str, terminal: bool, retryable: bool
) -> None:
    frame = _operation(state, 1)
    payload = json.loads(frame.to_json())

    assert payload["state"] == state
    assert payload["terminal"] is terminal
    assert payload["retryable"] is retryable
    assert payload["updated_at"] == "2026-07-16T12:00:01Z"


def test_operation_sequence_keeps_highest_revision_and_first_terminal() -> None:
    sequence = [
        _operation("accepted", 0),
        _operation("running", 1),
        _operation("accepted", 0),
        _operation("failed", 2),
        _operation("completed", 3),
    ]
    current = None
    for candidate in sequence:
        if current is not None and (
            current.terminal or candidate.sequence <= current.sequence
        ):
            continue
        current = candidate

    assert current is not None
    assert current.state == "failed"
    assert current.sequence == 2
    assert current.terminal is True


def test_operation_builder_rejects_a_noncanonical_flag_projection() -> None:
    with pytest.raises(ProtocolValidationError, match="disagree"):
        canonical_operation_status(
            operation_id=OPERATION_ID,
            action="curated_example",
            surface="chat",
            chat_id=None,
            connection_generation=CONNECTION_GENERATION,
            request_generation=REQUEST_GENERATION,
            sequence=0,
            state="running",
            phase="running",
            label="Running",
            terminal=True,
            retryable=False,
            error=None,
            retry_after_ms=None,
        )


@pytest.mark.asyncio
async def test_operation_emitter_uses_safe_send_and_canonical_json() -> None:
    orchestrator = _Orchestrator()
    sent = await emit_operation_status(
        orchestrator,
        orchestrator.owner_first,
        operation_id=OPERATION_ID,
        action="curated_example",
        surface="chat",
        chat_id=None,
        connection_generation=CONNECTION_GENERATION,
        request_generation=REQUEST_GENERATION,
        sequence=4,
        state="completed",
        phase="completed",
        label="Completed",
        terminal=True,
        retryable=False,
        error=None,
        retry_after_ms=None,
    )

    assert sent is True
    assert orchestrator.sent[0][1]["type"] == "operation_status"
    assert orchestrator.sent[0][1]["state"] == "completed"


@pytest.mark.parametrize(
    ("state", "revision_id", "runtime_id", "reason"),
    [
        ("starting", REVISION_ID, RUNTIME_ID, None),
        ("online", REVISION_ID, RUNTIME_ID, None),
        ("updating", REVISION_ID, RUNTIME_ID, None),
        ("failed", REVISION_ID, None, "child_exited"),
        ("offline", REVISION_ID, None, "host_lost"),
    ],
)
def test_lifecycle_builder_covers_all_five_canonical_states(
    state: str,
    revision_id: str,
    runtime_id: str | None,
    reason: str | None,
) -> None:
    frame = canonical_agent_lifecycle(
        agent_id="ua-dice",
        revision_id=revision_id,
        runtime_instance_id=runtime_id,
        lifecycle_generation=14,
        state_revision=3,
        state=state,
        reason_code=reason,
        updated_at=datetime(2026, 7, 16, 12, 0, 0),
    )

    assert frame.label == AGENT_LIFECYCLE_LABELS[state]
    assert frame.state == state
    assert frame.updated_at == "2026-07-16T12:00:00Z"


def test_twenty_lifecycle_sequences_converge_lexicographically() -> None:
    for lifecycle_generation in range(1, 21):
        frames = [
            canonical_agent_lifecycle(
                agent_id="ua-dice",
                revision_id=REVISION_ID,
                runtime_instance_id=(None if state in {"failed", "offline"} else RUNTIME_ID),
                lifecycle_generation=lifecycle_generation,
                state_revision=revision,
                state=state,
                reason_code=("host_lost" if state == "offline" else None),
            )
            for revision, state in enumerate(
                ("starting", "online", "updating", "failed", "offline")
            )
        ]
        delivery = [frames[0], frames[2], frames[1], frames[3], frames[4], frames[4]]
        current = None
        for candidate in delivery:
            pair = (candidate.lifecycle_generation, candidate.state_revision)
            if current is not None and pair <= (
                current.lifecycle_generation,
                current.state_revision,
            ):
                continue
            current = candidate
        assert current is frames[4]
        assert current.state == "offline"


@pytest.mark.asyncio
async def test_lifecycle_publisher_is_owner_scoped_and_uses_a_stable_snapshot() -> None:
    orchestrator = _Orchestrator()

    delivered = await publish_agent_lifecycle(
        orchestrator,
        "owner",
        agent_id="ua-dice",
        revision_id=REVISION_ID,
        runtime_instance_id=RUNTIME_ID,
        lifecycle_generation=14,
        state_revision=1,
        state="online",
        reason_code=None,
    )

    assert delivered == 2
    assert {websocket for websocket, _payload in orchestrator.sent} == {
        orchestrator.owner_first,
        orchestrator.owner_second,
    }
    assert all(payload["type"] == "agent_lifecycle" for _, payload in orchestrator.sent)


@pytest.mark.asyncio
async def test_lifecycle_publisher_refuses_an_empty_owner() -> None:
    with pytest.raises(ValueError, match="owner_user_id"):
        await publish_agent_lifecycle(
            _Orchestrator(),
            "",
            agent_id="ua-dice",
            revision_id=None,
            runtime_instance_id=None,
            lifecycle_generation=1,
            state_revision=0,
            state="offline",
            reason_code="host_lost",
        )


def test_active_lifecycle_state_requires_both_revision_and_runtime_ids() -> None:
    with pytest.raises(ProtocolValidationError, match="require revision"):
        canonical_agent_lifecycle(
            agent_id="ua-dice",
            revision_id=None,
            runtime_instance_id=None,
            lifecycle_generation=1,
            state_revision=0,
            state="online",
        )
