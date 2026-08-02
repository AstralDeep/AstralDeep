"""LiveKit authority-boundary tests for Feature 065."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from livekit.api.twirp_client import ServerError

from orchestrator.livekit_service import (
    LiveKitConfigError,
    LiveKitService,
    LiveKitSettings,
    LiveKitUnavailable,
)


NOW = datetime(2026, 7, 31, 18, 0, tzinfo=UTC)
SESSION = "00000000-0000-4000-8000-000000000001"


class FakeIssuer:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def issue(self, **claims: object) -> str:
        self.calls.append(dict(claims))
        return "synthetic-livekit-token-" + "x" * 48


class FakeAdmin:
    def __init__(self) -> None:
        self.probes = 0
        self.created: list[str] = []
        self.removed: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.closed = 0
        self.probe_error: Exception | None = None
        self.remove_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.probe_gate: asyncio.Event | None = None

    async def probe_room_service(self) -> None:
        self.probes += 1
        if self.probe_gate is not None:
            await self.probe_gate.wait()
        if self.probe_error is not None:
            raise self.probe_error

    async def create_room(self, room_name: str) -> None:
        self.created.append(room_name)

    async def remove_participant(self, room_name: str, identity: str) -> None:
        self.removed.append((room_name, identity))
        if self.remove_error is not None:
            raise self.remove_error

    async def delete_room(self, room_name: str) -> None:
        self.deleted.append(room_name)
        if self.delete_error is not None:
            raise self.delete_error

    async def close(self) -> None:
        self.closed += 1


def _settings(**changes: object) -> LiveKitSettings:
    values: dict[str, object] = {
        "internal_url": "https://livekit.internal",
        "public_url": "wss://voice.example.test",
        "api_key": "lk-api-key",
        "api_secret": "lk-api-secret-value-at-least-32-bytes",
        "environment": "production",
        "grant_ttl_seconds": 300,
        "readiness_ttl_seconds": 10,
        "operation_timeout_seconds": 3.0,
    }
    values.update(changes)
    return LiveKitSettings(**values)


def test_settings_are_explicit_fail_closed_and_redacted() -> None:
    settings = LiveKitSettings.from_environ(
        {
            "ASTRAL_ENV": "production",
            "LIVEKIT_INTERNAL_URL": "https://livekit.internal",
            "LIVEKIT_PUBLIC_URL": "wss://voice.example.test",
            "LIVEKIT_API_KEY": "lk-api-key",
            "LIVEKIT_API_SECRET": "lk-api-secret-value-at-least-32-bytes",
        }
    )
    assert settings.public_url == "wss://voice.example.test"
    assert "lk-api-key" not in repr(settings)
    assert "lk-api-secret" not in repr(settings)

    with pytest.raises(LiveKitConfigError, match="missing_livekit_configuration"):
        LiveKitSettings.from_environ({"ASTRAL_ENV": "production"})
    with pytest.raises(LiveKitConfigError, match="insecure_public_url"):
        LiveKitSettings.from_environ(
            {
                "ASTRAL_ENV": "production",
                "LIVEKIT_INTERNAL_URL": "https://livekit.internal",
                "LIVEKIT_PUBLIC_URL": "ws://voice.example.test",
                "LIVEKIT_API_KEY": "key",
                "LIVEKIT_API_SECRET": "secret-secret-secret-secret-secret-32",
            }
        )
    development = LiveKitSettings.from_environ(
        {
            "ASTRAL_ENV": "development",
            "LIVEKIT_INTERNAL_URL": "http://livekit:7880",
            "LIVEKIT_PUBLIC_URL": "ws://localhost:7880",
            "LIVEKIT_API_KEY": "key",
            "LIVEKIT_API_SECRET": "secret-secret-secret",
        }
    )
    assert development.environment == "development"
    with pytest.raises(LiveKitConfigError, match="weak_livekit_api_secret"):
        LiveKitSettings(
            internal_url="https://livekit.internal",
            public_url="wss://voice.example.test",
            api_key="key",
            api_secret="too-short",
            environment="production",
        )


@pytest.mark.parametrize(
    ("changes", "reason"),
    (
        ({"grant_ttl_seconds": 0}, "invalid_grant_ttl"),
        ({"grant_ttl_seconds": 301}, "invalid_grant_ttl"),
        ({"readiness_ttl_seconds": 0}, "invalid_readiness_ttl"),
        ({"readiness_ttl_seconds": 31}, "invalid_readiness_ttl"),
        ({"operation_timeout_seconds": 0}, "invalid_operation_timeout"),
        ({"operation_timeout_seconds": 10.01}, "invalid_operation_timeout"),
    ),
)
def test_launch_grant_readiness_and_operation_bounds_fail_closed(
    changes: dict[str, object], reason: str
) -> None:
    with pytest.raises(LiveKitConfigError, match=reason):
        LiveKitService(
            _settings(**changes),
            token_issuer=FakeIssuer(),
            admin=FakeAdmin(),
            clock=lambda: NOW,
        )


def test_client_grant_is_short_lived_room_scoped_and_least_privilege() -> None:
    issuer = FakeIssuer()
    service = LiveKitService(
        _settings(),
        token_issuer=issuer,
        admin=FakeAdmin(),
        clock=lambda: NOW,
    )

    grant = service.mint_client_grant(
        grant_id="grant-client-1",
        session_id=SESSION,
        generation=2,
        media_grant_revision=3,
        room_name="voice-room-1",
        participant_identity="client-device-rev-3",
        worker_identity="voice-worker-a",
        issued_at=NOW,
    )

    assert grant["transport"] == "livekit"
    assert grant["expires_at"] == "2026-07-31T18:05:00Z"
    assert grant["url"] == "wss://voice.example.test"
    assert grant["join_token"].startswith("synthetic-livekit-token")
    assert issuer.calls == [
        {
            "room_name": "voice-room-1",
            "identity": "client-device-rev-3",
            "issued_at": NOW,
            "ttl_seconds": 300,
            "can_publish": True,
            "can_subscribe": True,
            "can_publish_data": False,
            "can_publish_microphone": True,
        }
    ]
    assert "lk-api-secret" not in repr(service)


def test_worker_grant_is_separate_and_never_contains_api_secret() -> None:
    issuer = FakeIssuer()
    service = LiveKitService(
        _settings(),
        token_issuer=issuer,
        admin=FakeAdmin(),
        clock=lambda: NOW,
    )

    grant = service.mint_worker_grant(
        revision=4,
        room_name="voice-room-1",
        worker_identity="voice-worker-a",
        issued_at=NOW,
    )

    assert grant == {
        "revision": 4,
        "livekit_url": "wss://livekit.internal",
        "join_token": "synthetic-livekit-token-" + "x" * 48,
        "issued_at": "2026-07-31T18:00:00Z",
        "expires_at": "2026-07-31T18:05:00Z",
        "room_name": "voice-room-1",
        "worker_identity": "voice-worker-a",
    }
    assert issuer.calls[0]["identity"] == "voice-worker-a"
    assert issuer.calls[0]["can_publish_microphone"] is True
    assert issuer.calls[0]["can_publish_data"] is True
    assert "lk-api-secret-value-at-least-32-bytes" not in repr(grant)


@pytest.mark.asyncio
async def test_rotation_removes_stale_identity_before_returning_new_grant() -> None:
    admin = FakeAdmin()
    issuer = FakeIssuer()
    service = LiveKitService(
        _settings(), token_issuer=issuer, admin=admin, clock=lambda: NOW
    )

    grant = await service.rotate_client_participant(
        previous_identity="client-old",
        grant_id="grant-client-2",
        session_id=SESSION,
        generation=2,
        media_grant_revision=4,
        room_name="voice-room-1",
        participant_identity="client-new",
        worker_identity="voice-worker-a",
        issued_at=NOW,
    )

    assert admin.removed == [("voice-room-1", "client-old")]
    assert grant["participant_identity"] == "client-new"


@pytest.mark.asyncio
async def test_readiness_is_bounded_cached_and_content_free() -> None:
    admin = FakeAdmin()
    now = NOW
    service = LiveKitService(
        _settings(), admin=admin, token_issuer=FakeIssuer(), clock=lambda: now
    )

    first = await service.readiness()
    second = await service.readiness()
    assert first == second
    assert first.status == "ready"
    assert admin.probes == 1

    now += timedelta(seconds=11)
    admin.probe_error = RuntimeError("secret provider body")
    failed = await service.readiness()
    assert failed.status == "unavailable"
    assert failed.reason == "media_unreachable"
    assert "secret provider body" not in repr(failed)
    assert admin.probes == 2


@pytest.mark.asyncio
async def test_readiness_timeout_and_admin_errors_are_typed_redacted() -> None:
    admin = FakeAdmin()
    admin.probe_gate = asyncio.Event()
    service = LiveKitService(
        replace(_settings(), operation_timeout_seconds=0.01),
        admin=admin,
        token_issuer=FakeIssuer(),
        clock=lambda: NOW,
    )
    status = await service.readiness()
    assert status.status == "unavailable"
    assert status.reason == "media_unreachable"

    with pytest.raises(LiveKitUnavailable, match="participant_removal_failed") as caught:
        bad = FakeAdmin()

        async def fail(*_args: object) -> None:
            raise RuntimeError("api secret leaked upstream")

        bad.remove_participant = fail  # type: ignore[method-assign]
        failed_service = LiveKitService(
            _settings(), admin=bad, token_issuer=FakeIssuer(), clock=lambda: NOW
        )
        await failed_service.remove_participant("voice-room-1", "client-old")
    assert "api secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_room_cleanup_is_bounded_and_closes_owned_admin() -> None:
    admin = FakeAdmin()
    service = LiveKitService(
        _settings(), admin=admin, token_issuer=FakeIssuer(), clock=lambda: NOW
    )
    await service.ensure_room("voice-room-1")
    await service.delete_room("voice-room-1")
    await service.close()
    assert admin.created == ["voice-room-1"]
    assert admin.deleted == ["voice-room-1"]
    assert admin.closed == 1


@pytest.mark.asyncio
async def test_room_cleanup_treats_typed_not_found_as_retry_success() -> None:
    admin = FakeAdmin()
    admin.remove_error = ServerError(
        "not_found",
        "participant detail must remain private",
        status=404,
    )
    admin.delete_error = ServerError(
        "not_found",
        "room detail must remain private",
        status=404,
    )
    service = LiveKitService(
        _settings(), admin=admin, token_issuer=FakeIssuer(), clock=lambda: NOW
    )

    await service.remove_participant("voice-room-1", "client-old")
    await service.delete_room("voice-room-1")

    assert admin.removed == [("voice-room-1", "client-old")]
    assert admin.deleted == ["voice-room-1"]


@pytest.mark.asyncio
async def test_room_cleanup_does_not_swallow_untyped_or_non_404_errors() -> None:
    admin = FakeAdmin()
    admin.remove_error = ServerError("permission_denied", "private", status=403)
    service = LiveKitService(
        _settings(), admin=admin, token_issuer=FakeIssuer(), clock=lambda: NOW
    )

    with pytest.raises(LiveKitUnavailable, match="participant_removal_failed"):
        await service.remove_participant("voice-room-1", "client-old")


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("room_name", "bad room"),
        ("participant_identity", "bad/participant"),
        ("worker_identity", ""),
        ("generation", 0),
        ("media_grant_revision", -1),
    ),
)
def test_grant_claim_validation_fails_before_token_issuance(
    field: str, value: object
) -> None:
    issuer = FakeIssuer()
    service = LiveKitService(
        _settings(), admin=FakeAdmin(), token_issuer=issuer, clock=lambda: NOW
    )
    args: dict[str, object] = {
        "grant_id": "grant-client-1",
        "session_id": SESSION,
        "generation": 1,
        "media_grant_revision": 1,
        "room_name": "voice-room-1",
        "participant_identity": "client-1",
        "worker_identity": "worker-1",
        "issued_at": NOW,
    }
    args[field] = value
    with pytest.raises(ValueError):
        service.mint_client_grant(**args)  # type: ignore[arg-type]
    assert issuer.calls == []
