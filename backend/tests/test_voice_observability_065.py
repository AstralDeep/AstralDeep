"""Feature 065 kill-switch and content-free telemetry boundary tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from orchestrator.api import _runtime_metric_snapshot
from orchestrator.livekit_service import LiveKitReadiness, VoiceCapabilityService
from orchestrator.runtime_observability import RuntimeObservability
from shared.feature_flags import FeatureFlags


def test_conversational_voice_is_included_by_default_and_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FF_CONVERSATIONAL_VOICE", raising=False)
    assert FeatureFlags().is_enabled("conversational_voice")

    monkeypatch.setenv("FF_CONVERSATIONAL_VOICE", "false")
    assert not FeatureFlags().is_enabled("conversational_voice")


def test_voice_metrics_accept_only_content_free_reviewed_dimensions() -> None:
    metrics = RuntimeObservability(deployment_instance="test")

    metrics.record_voice_state(
        state="listening",
        reason="none",
        client_kind="web",
        transport="livekit",
    )
    metrics.observe_voice_timing(
        "cadence_gap",
        14.25,
        client_kind="web",
        transport="livekit",
    )
    metrics.observe_voice_count(
        "sessions",
        2,
        client_kind="web",
        transport="livekit",
    )
    metrics.record_voice_cleanup(
        "complete",
        client_kind="web",
        transport="livekit",
    )
    metrics.record_voice_event(
        "tts",
        "failed",
        reason="speech_unavailable",
        client_kind="web",
        transport="livekit",
    )

    snapshot = metrics.snapshot()
    assert {sample.name for sample in snapshot} == {
        "voice_cadence_gap_seconds",
        "voice_cleanup_total",
        "voice_sessions",
        "voice_state_transition_total",
        "voice_tts_total",
    }
    rendered = repr(snapshot)
    assert "transcript" not in rendered
    assert "session_id" not in rendered


@pytest.mark.asyncio
async def test_production_readiness_reaches_single_exposed_runtime_snapshot() -> None:
    voice_metrics = RuntimeObservability(deployment_instance="test")
    primary_metrics = RuntimeObservability(deployment_instance="test")

    class _LiveKit:
        async def readiness(self) -> LiveKitReadiness:
            return LiveKitReadiness(
                status="ready",
                reason="ready",
                checked_at="2026-08-01T12:00:00Z",
                expires_at="2026-08-01T12:00:10Z",
            )

    workers = SimpleNamespace(
        readiness=lambda: SimpleNamespace(
            ready=True,
            reason="ready",
            worker_count=1,
            capacity_available=1,
            profile={
                "asr_model": "Systran/faster-whisper-large-v3",
                "tts_model": "speaches-ai/Kokoro-82M-v1.0-ONNX",
                "voice": "af_heart",
                "output_locale": "en-US",
                "format": "wav",
                "sample_rate_hz": 24_000,
            },
        )
    )
    service = VoiceCapabilityService(
        livekit=_LiveKit(),  # type: ignore[arg-type]
        workers=workers,
        feature_enabled=lambda: True,
        clock=lambda: datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
        observability=voice_metrics,
    )
    assert (await service.readiness()).status == "ready"

    orchestrator = SimpleNamespace(
        runtime_observability=primary_metrics,
        voice_services=SimpleNamespace(observability=voice_metrics),
    )
    snapshot = _runtime_metric_snapshot(orchestrator)
    readiness = [sample for sample in snapshot if sample.name == "voice_readiness_total"]
    assert len(readiness) == 1
    assert readiness[0].value == 1
    assert readiness[0].labels == {
        "deployment_instance": "test",
        "result_code": "ready",
        "voice_reason": "ready",
    }

    # A future shared collector must not cause the same exact series to be
    # exported or counted twice.
    primary_metrics.record_voice_event("readiness", "ready", reason="ready")
    snapshot = _runtime_metric_snapshot(orchestrator)
    assert sum(sample.name == "voice_readiness_total" for sample in snapshot) == 1


@pytest.mark.parametrize(
    ("method", "value"),
    (
        ("state", "user_secret_words"),
        ("reason", "https://speech.internal"),
        ("client", "device_9b6bcd09"),
        ("transport", "Bearer_secret"),
        ("timing", "custom_user_timing"),
        ("count", "chat_1234"),
        ("cleanup", "user_requested_because_secret"),
        ("event", "session_identifier"),
        ("outcome", "provider_response_body"),
        ("event_reason", "https://speech.internal"),
    ),
)
def test_voice_metric_vocabulary_rejects_arbitrary_or_sensitive_values(
    method: str,
    value: str,
) -> None:
    metrics = RuntimeObservability(deployment_instance="test")

    with pytest.raises(ValueError, match="reviewed voice vocabulary"):
        if method == "state":
            metrics.record_voice_state(
                state=value,
                reason="none",
                client_kind="web",
                transport="livekit",
            )
        elif method == "reason":
            metrics.record_voice_state(
                state="off",
                reason=value,
                client_kind="web",
                transport="livekit",
            )
        elif method == "client":
            metrics.record_voice_state(
                state="off",
                reason="none",
                client_kind=value,
                transport="livekit",
            )
        elif method == "transport":
            metrics.record_voice_state(
                state="off",
                reason="none",
                client_kind="web",
                transport=value,
            )
        elif method == "timing":
            metrics.observe_voice_timing(
                value,
                1.0,
                client_kind="web",
                transport="livekit",
            )
        elif method == "count":
            metrics.observe_voice_count(
                value,
                1,
                client_kind="web",
                transport="livekit",
            )
        elif method == "cleanup":
            metrics.record_voice_cleanup(
                value,
                client_kind="web",
                transport="livekit",
            )
        elif method == "event":
            metrics.record_voice_event(value, "started")
        elif method == "outcome":
            metrics.record_voice_event("session", value)
        else:
            metrics.record_voice_event("session", "started", reason=value)


def test_voice_event_dimensions_are_both_present_or_both_absent() -> None:
    metrics = RuntimeObservability(deployment_instance="test")
    with pytest.raises(ValueError, match="must be paired"):
        metrics.record_voice_event(
            "session",
            "started",
            client_kind="web",
        )
