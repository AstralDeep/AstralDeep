"""Tests for orchestrator/voice_recap.py's sensitive-result consent: owner-and-result-bound
one-time consent, strict spoken control, and expiry of the memory-only sensitive registry.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.voice_recap import (
    SensitiveRecapRegistry,
    SensitiveResultConsent,
    SpokenControlContext,
    VoiceRecapError,
    resolve_spoken_control,
)


NOW = datetime(2026, 7, 31, 19, 0, tzinfo=UTC)
RESULT_A = "00000000-0000-4000-8000-000000000011"
RESULT_B = "00000000-0000-4000-8000-000000000012"








@pytest.mark.parametrize("method", ["tap", "strict_spoken_control"])
def test_consent_is_exactly_owner_and_result_bound_for_both_methods(method: str) -> None:
    consent = SensitiveResultConsent.issue(
        user_id="owner",
        result_id=RESULT_A,
        method=method,  # type: ignore[arg-type]
        now=NOW,
    )
    with pytest.raises(VoiceRecapError, match="scope_mismatch"):
        consent.consume(user_id="other", result_id=RESULT_A, now=NOW)
    with pytest.raises(VoiceRecapError, match="scope_mismatch"):
        consent.consume(user_id="owner", result_id=RESULT_B, now=NOW)


def test_consent_is_fresh_one_time_and_replay_safe() -> None:
    consent = SensitiveResultConsent.issue(
        user_id="owner",
        result_id=RESULT_A,
        method="tap",
        now=NOW,
    )
    with pytest.raises(VoiceRecapError, match="expired"):
        consent.consume(
            user_id="owner",
            result_id=RESULT_A,
            now=NOW + timedelta(minutes=2),
        )
    consumed = consent.consume(
        user_id="owner",
        result_id=RESULT_A,
        now=NOW + timedelta(seconds=20),
    )
    with pytest.raises(VoiceRecapError, match="already_consumed"):
        consumed.consume(
            user_id="owner",
            result_id=RESULT_A,
            now=NOW + timedelta(seconds=21),
        )


def test_only_exact_unambiguous_spoken_read_control_can_create_consent_intent() -> None:
    context = SpokenControlContext(pending_sensitive_result_id=RESULT_A)
    resolved = resolve_spoken_control("Read it.", context)
    assert resolved is not None
    assert resolved.action == "read_sensitive_result"
    assert resolved.target_id == RESULT_A
    for ambiguous in (
        "please read it",
        "read a result",
        "read the other one",
        "read it and send it",
        "read it twice",
    ):
        assert resolve_spoken_control(ambiguous, context) is None
    assert resolve_spoken_control("read it", SpokenControlContext()) is None




@pytest.mark.asyncio
async def test_memory_only_sensitive_registry_is_exactly_scoped_and_one_use() -> None:
    registry = SensitiveRecapRegistry(capacity=2)
    session_id = "00000000-0000-4000-8000-000000000021"
    turn_id = "00000000-0000-4000-8000-000000000022"
    await registry.remember(
        user_id="owner",
        session_id=session_id,
        generation=1,
        media_grant_revision=2,
        turn_id=turn_id,
        result_id=RESULT_A,
        text="The sensitive result details.",
        now=NOW,
    )
    assert registry.retained_count == 1
    text = await registry.consume(
        user_id="owner",
        session_id=session_id,
        generation=1,
        media_grant_revision=2,
        turn_id=turn_id,
        result_id=RESULT_A,
        now=NOW + timedelta(seconds=1),
    )
    assert text == "The sensitive result details."
    assert registry.retained_count == 0
    with pytest.raises(VoiceRecapError, match="sensitive_consent_unavailable"):
        await registry.consume(
            user_id="owner",
            session_id=session_id,
            generation=1,
            media_grant_revision=2,
            turn_id=turn_id,
            result_id=RESULT_A,
            now=NOW + timedelta(seconds=2),
        )


@pytest.mark.asyncio
async def test_sensitive_registry_expires_and_zeroes_all_entries_on_clear() -> None:
    registry = SensitiveRecapRegistry(capacity=1)
    await registry.remember(
        user_id="owner",
        session_id="00000000-0000-4000-8000-000000000031",
        generation=1,
        media_grant_revision=1,
        turn_id="00000000-0000-4000-8000-000000000032",
        result_id=RESULT_A,
        text="Short-lived details.",
        now=NOW,
        lifetime=timedelta(seconds=1),
    )
    with pytest.raises(VoiceRecapError, match="sensitive_consent_unavailable"):
        await registry.consume(
            user_id="owner",
            session_id="00000000-0000-4000-8000-000000000031",
            generation=1,
            media_grant_revision=1,
            turn_id="00000000-0000-4000-8000-000000000032",
            result_id=RESULT_A,
            now=NOW + timedelta(seconds=1),
        )
    assert registry.retained_count == 0
    await registry.remember(
        user_id="owner",
        session_id="00000000-0000-4000-8000-000000000031",
        generation=1,
        media_grant_revision=1,
        turn_id="00000000-0000-4000-8000-000000000032",
        result_id=RESULT_A,
        text="Fresh details.",
        now=NOW,
    )
    await registry.clear()
    assert registry.retained_count == 0
