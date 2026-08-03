"""Sensitive-result disclosure gates for Feature 065."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.voice_recap import (
    SENSITIVE_NOTICE,
    SensitiveRecapRegistry,
    SensitiveResultConsent,
    SpokenControlContext,
    VoiceRecapError,
    apply_sensitivity_policy,
    build_spoken_recap,
    resolve_spoken_control,
)


NOW = datetime(2026, 7, 31, 19, 0, tzinfo=UTC)
RESULT_A = "00000000-0000-4000-8000-000000000011"
RESULT_B = "00000000-0000-4000-8000-000000000012"


def _recap():
    return build_spoken_recap(
        authoritative_summary="The patient result is available.",
        committed_components=None,
        detected_language="en",
    )


@pytest.mark.parametrize("classification", ["sensitive", "phi", "unknown", ""])
def test_unknown_or_sensitive_classification_speaks_only_generic_notice(
    classification: str,
) -> None:
    gated = apply_sensitivity_policy(
        _recap(),
        confidentiality=classification,
        contains_phi=lambda _text: False,
    )
    assert gated.text == SENSITIVE_NOTICE
    assert gated.output_reason == "sensitive_consent_required"
    assert "patient" not in gated.text.lower()


def test_phi_detector_positive_or_failure_fails_closed() -> None:
    detected = apply_sensitivity_policy(
        _recap(),
        confidentiality="non_sensitive",
        contains_phi=lambda _text: True,
    )

    def unavailable(_text: str) -> bool:
        raise TimeoutError

    failed = apply_sensitivity_policy(
        _recap(),
        confidentiality="non_sensitive",
        contains_phi=unavailable,
    )
    assert detected.text == SENSITIVE_NOTICE
    assert failed.text == SENSITIVE_NOTICE


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


def test_details_are_released_only_after_the_bound_consent_is_consumed() -> None:
    gated = apply_sensitivity_policy(
        _recap(),
        confidentiality="sensitive",
        contains_phi=lambda _text: True,
    )
    consent = SensitiveResultConsent.issue(
        user_id="owner",
        result_id=RESULT_A,
        method="strict_spoken_control",
        now=NOW,
    ).consume(
        user_id="owner",
        result_id=RESULT_A,
        now=NOW + timedelta(seconds=1),
    )
    released = apply_sensitivity_policy(
        _recap(),
        confidentiality="sensitive",
        contains_phi=lambda _text: True,
        consent_granted=consent.consumed_at is not None,
    )
    assert gated.text == SENSITIVE_NOTICE
    assert released.text == "The patient result is available"
    assert released.sensitivity == "sensitive"


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
