"""Tests that the voice turn-state success frame reports the source outcome and never claims
audibility.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from orchestrator.orchestrator import (
    Orchestrator,
    _VOICE_REQUEST_SUCCEEDED_MESSAGE,
)
from orchestrator.tests.test_voice_bootstrap_065 import (
    _voice_turn,
)


def test_success_frame_reports_source_outcome_without_claiming_audibility() -> None:
    turn = replace(
        _voice_turn(
            state="succeeded",
            result_commit_id=str(uuid.uuid4()),
        ),
        is_foreground=False,
    )

    frame = Orchestrator._voice_turn_state_frame(
        turn,
        connection_generation=str(uuid.uuid4()),
        message=_VOICE_REQUEST_SUCCEEDED_MESSAGE,
        speech_outcome="source_finished",
    )

    assert frame["state"] == "succeeded"
    assert frame["speech_outcome"] == "source_finished"
    assert "audible" not in str(frame).lower()

    with pytest.raises(ValueError, match="invalid_voice_turn_speech_outcome"):
        Orchestrator._voice_turn_state_frame(
            replace(turn, state="failed"),
            connection_generation=str(uuid.uuid4()),
            message="failed",
            speech_outcome="source_finished",
        )
