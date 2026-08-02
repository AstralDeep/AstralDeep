"""Bounded exact-profile capability tests for Feature 065."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.livekit_service import (
    LiveKitReadiness,
    VoiceCapabilityService,
)


NOW = datetime(2026, 7, 31, 20, 0, tzinfo=UTC)
PROFILE = {
    "asr_model": "Systran/faster-whisper-large-v3",
    "tts_model": "speaches-ai/Kokoro-82M-v1.0-ONNX",
    "voice": "af_heart",
    "output_locale": "en-US",
    "format": "wav",
    "sample_rate_hz": 24_000,
}


@dataclass
class FakeLiveKit:
    status: str = "ready"
    calls: int = 0
    gate: asyncio.Event | None = None

    async def readiness(self) -> LiveKitReadiness:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        return LiveKitReadiness(
            status=self.status,
            reason="ready" if self.status == "ready" else "media_unreachable",
            checked_at="2026-07-31T20:00:00Z",
            expires_at="2026-07-31T20:00:10Z",
        )


@dataclass
class FakeWorkerReadiness:
    ready: bool = True
    reason: str = "ready"
    worker_count: int = 1
    capacity_available: int = 5
    profile: dict[str, str | int] = field(default_factory=lambda: dict(PROFILE))


@dataclass
class FakeWorkers:
    value: FakeWorkerReadiness = field(default_factory=FakeWorkerReadiness)
    calls: int = 0

    def readiness(self) -> FakeWorkerReadiness:
        self.calls += 1
        return self.value


def _service(
    *,
    livekit: FakeLiveKit | None = None,
    workers: FakeWorkers | None = None,
    enabled=lambda: True,
    now: list[datetime] | None = None,
) -> VoiceCapabilityService:
    clock = now or [NOW]
    return VoiceCapabilityService(
        livekit=livekit or FakeLiveKit(),  # type: ignore[arg-type]
        workers=workers or FakeWorkers(),
        feature_enabled=enabled,
        clock=lambda: clock[0],
    )


@pytest.mark.asyncio
async def test_ready_response_is_exact_redacted_and_contract_shaped() -> None:
    result = (await _service().readiness()).to_dict()
    assert result == {
        "schema_version": "1",
        "status": "ready",
        "reason": "ready",
        "checked_at": "2026-07-31T20:00:00Z",
        "expires_at": "2026-07-31T20:00:10Z",
        "profile": {
            "asr_model": "Systran/faster-whisper-large-v3",
            "tts_model": "speaches-ai/Kokoro-82M-v1.0-ONNX",
            "voice": "af_heart",
            "output_locale": "en-US",
            "output_format": "wav",
            "output_sample_rate_hz": 24_000,
        },
        "supported_transports": ["livekit"],
        "components": {
            "livekit": "ready",
            "worker": "ready",
            "asr": "ready",
            "tts": "ready",
            "voice": "ready",
        },
    }
    assert "url" not in result and "key" not in repr(result).lower()


@pytest.mark.asyncio
async def test_feature_disabled_does_not_probe_dependencies() -> None:
    livekit = FakeLiveKit()
    workers = FakeWorkers()
    result = await _service(
        livekit=livekit,
        workers=workers,
        enabled=lambda: False,
    ).readiness()
    assert result.reason == "feature_disabled"
    assert set(result.components.values()) == {"unavailable"}
    assert livekit.calls == workers.calls == 0


@pytest.mark.asyncio
async def test_media_worker_and_capacity_failures_have_stable_precedence() -> None:
    media = await _service(livekit=FakeLiveKit(status="unavailable")).readiness()
    assert media.status == "unavailable" and media.reason == "media_unreachable"

    absent_workers = FakeWorkers(
        FakeWorkerReadiness(
            ready=False,
            reason="worker_unavailable",
            worker_count=0,
            capacity_available=0,
        )
    )
    absent = await _service(workers=absent_workers).readiness()
    assert absent.status == "unavailable" and absent.reason == "worker_unavailable"

    full_workers = FakeWorkers(
        FakeWorkerReadiness(
            ready=False,
            reason="capacity_exhausted",
            worker_count=1,
            capacity_available=0,
        )
    )
    full = await _service(workers=full_workers).readiness()
    assert full.status == "degraded" and full.reason == "capacity_exhausted"
    assert set(full.components.values()) == {"ready"}


@pytest.mark.asyncio
async def test_profile_drift_never_advertises_asr_tts_or_voice() -> None:
    workers = FakeWorkers()
    workers.value.profile["voice"] = "wrong_voice"
    result = await _service(workers=workers).readiness()
    assert result.reason == "worker_unavailable"
    assert result.components["livekit"] == "ready"
    assert result.components["asr"] == "unavailable"
    assert result.components["tts"] == "unavailable"
    assert result.components["voice"] == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "component"),
    (
        ("asr_unavailable", "asr"),
        ("tts_unavailable", "tts"),
        ("voice_unavailable", "voice"),
    ),
)
async def test_worker_preflight_failure_preserves_exact_component_reason(
    reason: str,
    component: str,
) -> None:
    workers = FakeWorkers(
        FakeWorkerReadiness(ready=False, reason=reason)
    )
    result = await _service(workers=workers).readiness()
    assert result.status == "unavailable"
    assert result.reason == reason
    assert result.components[component] == "unavailable"


@pytest.mark.asyncio
async def test_cache_is_bounded_expires_and_can_be_invalidated() -> None:
    now = [NOW]
    livekit = FakeLiveKit()
    workers = FakeWorkers()
    service = _service(livekit=livekit, workers=workers, now=now)
    first = await service.readiness()
    assert await service.readiness() is first
    assert livekit.calls == workers.calls == 1
    now[0] += timedelta(seconds=10)
    second = await service.readiness()
    assert second is not first
    assert livekit.calls == workers.calls == 2
    service.invalidate()
    await service.readiness()
    assert livekit.calls == workers.calls == 3


@pytest.mark.asyncio
async def test_concurrent_cold_start_is_coalesced() -> None:
    gate = asyncio.Event()
    livekit = FakeLiveKit(gate=gate)
    workers = FakeWorkers()
    service = _service(livekit=livekit, workers=workers)
    calls = [asyncio.create_task(service.readiness()) for _ in range(8)]
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*calls)
    assert all(item is results[0] for item in results)
    assert livekit.calls == workers.calls == 1


@pytest.mark.asyncio
async def test_probe_and_flag_errors_fail_closed_without_error_content() -> None:
    async def broken_probe():
        raise RuntimeError("secret endpoint body")

    service = VoiceCapabilityService(
        livekit=FakeLiveKit(),  # type: ignore[arg-type]
        workers=FakeWorkers(),
        feature_enabled=lambda: True,
        clock=lambda: NOW,
        worker_probe=broken_probe,
    )
    failed = await service.readiness()
    assert failed.reason == "internal_error"
    assert "secret" not in repr(failed)

    flag_failed = await _service(
        enabled=lambda: (_ for _ in ()).throw(RuntimeError("secret"))
    ).readiness()
    assert flag_failed.reason == "feature_disabled"


@pytest.mark.parametrize("ttl", [0, 31])
def test_cache_bounds_are_enforced(ttl: int) -> None:
    with pytest.raises(ValueError, match="invalid_capability_cache_ttl"):
        VoiceCapabilityService(
            livekit=FakeLiveKit(),  # type: ignore[arg-type]
            workers=FakeWorkers(),
            feature_enabled=lambda: True,
            cache_ttl_seconds=ttl,
        )
