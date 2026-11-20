"""Tests for orchestrator/livekit_service.py: fail-closed redacted settings, room/grant
bounds, least-privilege client vs. worker token grants, identity rotation, cached
readiness, and typed room-cleanup error handling.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from orchestrator.livekit_service import (
    LiveKitConfigError,
    LiveKitSettings,
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


