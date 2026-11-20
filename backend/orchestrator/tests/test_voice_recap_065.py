"""Tests for orchestrator/voice_recap.py's sensitive-result consent lifetime and scope and its
strict spoken-control resolution.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.voice_recap import (
    SensitiveResultConsent,
    SpokenControlContext,
    VoiceRecapError,
    resolve_spoken_control,
)


NOW = datetime(2026, 7, 31, 18, 0, tzinfo=UTC)
RESULT = "00000000-0000-4000-8000-000000000001"






















def test_sensitive_consent_is_fresh_owner_result_bound_and_one_time() -> None:
    consent = SensitiveResultConsent.issue(
        user_id="user-a",
        result_id=RESULT,
        method="strict_spoken_control",
        now=NOW,
    )
    with pytest.raises(VoiceRecapError, match="scope_mismatch"):
        consent.consume(user_id="user-b", result_id=RESULT, now=NOW)
    with pytest.raises(VoiceRecapError, match="scope_mismatch"):
        consent.consume(user_id="user-a", result_id="other", now=NOW)
    with pytest.raises(VoiceRecapError, match="expired"):
        consent.consume(
            user_id="user-a",
            result_id=RESULT,
            now=NOW + timedelta(minutes=2),
        )
    consumed = consent.consume(
        user_id="user-a",
        result_id=RESULT,
        now=NOW + timedelta(seconds=1),
    )
    with pytest.raises(VoiceRecapError, match="already_consumed"):
        consumed.consume(
            user_id="user-a",
            result_id=RESULT,
            now=NOW + timedelta(seconds=2),
        )


def test_strict_spoken_control_resolves_only_when_exact_context_exists() -> None:
    context = SpokenControlContext(
        pending_sensitive_result_id=RESULT,
        speech_active=True,
        voice_active=True,
        foreground_task_id="task-1",
    )
    assert resolve_spoken_control(" Read it! ", context).action == "read_sensitive_result"
    assert resolve_spoken_control("stop speaking", context).action == "stop_speech"
    assert resolve_spoken_control("mute voice", context).action == "mute_voice"
    assert resolve_spoken_control("cancel my request", context).target_id == "task-1"
    for ordinary in (
        "read the latest result",
        "please read it",
        "mute the television",
        "cancel all requests",
        "Ｒｅａｄ it",
    ):
        assert resolve_spoken_control(ordinary, context) is None
    assert resolve_spoken_control("read it", SpokenControlContext()) is None
    assert resolve_spoken_control("stop speaking", SpokenControlContext()) is None


def test_invalid_consent_lifetime_fails_closed() -> None:
    with pytest.raises(VoiceRecapError, match="invalid_consent_lifetime"):
        SensitiveResultConsent.issue(
            user_id="user-a",
            result_id=RESULT,
            method="tap",
            now=NOW,
            lifetime=timedelta(minutes=3),
        )
