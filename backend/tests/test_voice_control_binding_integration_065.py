"""Authenticated register/teardown integration for Feature-065 bindings."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from orchestrator.orchestrator import Orchestrator
from orchestrator.voice_control_binding import (
    VoiceControlBindingError,
    VoiceControlBindingIssuer,
)
from shared.protocol import RegisterUI


NOW = datetime(2026, 7, 31, 18, 0, tzinfo=UTC)
DEVICE = "00000000-0000-4000-8000-000000000001"
CONNECTION = "00000000-0000-4000-8000-000000000002"


class _NonClosingStringIO(io.StringIO):
    def close(self) -> None:
        self.flush()


def _orchestrator() -> Orchestrator:
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._voice_binding_issuer = VoiceControlBindingIssuer(
        b"v" * 32,
        clock=lambda: NOW,
    )
    orchestrator._voice_control_bindings = {}
    orchestrator._safe_send = AsyncMock(return_value=True)
    return orchestrator


def _registration(**changes: object) -> RegisterUI:
    values: dict[str, object] = {
        "device_id": DEVICE,
        "connection_generation": CONNECTION,
    }
    values.update(changes)
    return RegisterUI(**values)


@pytest.mark.asyncio
async def test_authenticated_registration_delivers_once_and_retains_no_bearer() -> None:
    orchestrator = _orchestrator()
    websocket = object()
    claims = {
        "sub": "user-a",
        "exp": int((NOW + timedelta(minutes=4)).timestamp()),
    }

    assert await orchestrator._issue_voice_control_binding(
        websocket,
        _registration(),
        claims,
    )

    wire = orchestrator._safe_send.await_args.args[1]
    frame = json.loads(wire)
    retained = orchestrator._voice_control_bindings[id(websocket)]
    assert frame["type"] == "voice_control_binding"
    assert frame["device_id"] == DEVICE
    assert frame["connection_generation"] == CONNECTION
    assert frame["binding_id"] == retained.binding_id
    assert frame["expires_at"] == "2026-07-31T18:04:00Z"
    assert frame["binding"] not in repr(retained)
    assert frame["binding"] not in repr(orchestrator._voice_control_bindings)


@pytest.mark.asyncio
async def test_real_clock_precision_binding_is_immediately_rest_valid() -> None:
    fractional_now = NOW.replace(microsecond=987_654)
    orchestrator = _orchestrator()
    orchestrator._voice_binding_issuer = VoiceControlBindingIssuer(
        b"v" * 32,
        clock=lambda: fractional_now,
    )
    websocket = object()

    assert await orchestrator._issue_voice_control_binding(
        websocket,
        _registration(),
        {
            "sub": "user-a",
            "exp": int((NOW + timedelta(minutes=4)).timestamp()),
        },
    )

    frame = json.loads(orchestrator._safe_send.await_args.args[1])
    retained = orchestrator._voice_control_bindings[id(websocket)]
    assert orchestrator.validate_voice_control_binding(
        bearer=frame["binding"],
        subject="user-a",
        device_id=DEVICE,
        connection_generation=CONNECTION,
    ) == retained


@pytest.mark.asyncio
async def test_binding_is_current_before_websocket_delivery_completes() -> None:
    orchestrator = _orchestrator()
    websocket = object()

    async def validate_during_send(_websocket: object, wire: str) -> bool:
        frame = json.loads(wire)
        assert orchestrator.validate_voice_control_binding(
            bearer=frame["binding"],
            subject="user-a",
            device_id=DEVICE,
            connection_generation=CONNECTION,
        ) == orchestrator._voice_control_bindings[id(websocket)]
        return True

    orchestrator._safe_send.side_effect = validate_during_send
    assert await orchestrator._issue_voice_control_binding(
        websocket,
        _registration(),
        {
            "sub": "user-a",
            "exp": int((NOW + timedelta(minutes=4)).timestamp()),
        },
    )


@pytest.mark.asyncio
async def test_failed_delivery_restores_prior_device_binding() -> None:
    orchestrator = _orchestrator()
    first_socket = object()
    second_socket = object()
    claims = {
        "sub": "user-a",
        "exp": int((NOW + timedelta(minutes=10)).timestamp()),
    }
    assert await orchestrator._issue_voice_control_binding(
        first_socket,
        _registration(),
        claims,
    )
    first_frame = json.loads(orchestrator._safe_send.await_args.args[1])

    orchestrator._safe_send.return_value = False
    assert not await orchestrator._issue_voice_control_binding(
        second_socket,
        _registration(
            connection_generation="00000000-0000-4000-8000-000000000003"
        ),
        claims,
    )

    assert id(second_socket) not in orchestrator._voice_control_bindings
    assert orchestrator._voice_device_bindings[("user-a", DEVICE)] == id(
        first_socket
    )
    assert orchestrator.validate_voice_control_binding(
        bearer=first_frame["binding"],
        subject="user-a",
        device_id=DEVICE,
        connection_generation=CONNECTION,
    ) == orchestrator._voice_control_bindings[id(first_socket)]


@pytest.mark.asyncio
async def test_reregistration_rotates_bearer_but_cannot_change_socket_scope() -> None:
    orchestrator = _orchestrator()
    websocket = object()
    claims = {
        "sub": "user-a",
        "exp": int((NOW + timedelta(minutes=10)).timestamp()),
    }
    await orchestrator._issue_voice_control_binding(websocket, _registration(), claims)
    first = json.loads(orchestrator._safe_send.await_args.args[1])

    await orchestrator._issue_voice_control_binding(websocket, _registration(), claims)
    second = json.loads(orchestrator._safe_send.await_args.args[1])

    assert second["binding_id"] != first["binding_id"]
    assert second["binding"] != first["binding"]
    with pytest.raises(VoiceControlBindingError, match="binding_scope_mismatch"):
        await orchestrator._issue_voice_control_binding(
            websocket,
            _registration(
                device_id="00000000-0000-4000-8000-000000000003"
            ),
            claims,
        )


@pytest.mark.asyncio
async def test_new_connection_for_same_user_device_fences_prior_socket() -> None:
    orchestrator = _orchestrator()
    first_socket = object()
    second_socket = object()
    claims = {
        "sub": "user-a",
        "exp": int((NOW + timedelta(minutes=10)).timestamp()),
    }
    await orchestrator._issue_voice_control_binding(
        first_socket,
        _registration(),
        claims,
    )
    first_frame = json.loads(orchestrator._safe_send.await_args.args[1])
    await orchestrator._issue_voice_control_binding(
        second_socket,
        _registration(connection_generation="00000000-0000-4000-8000-000000000003"),
        claims,
    )

    assert id(first_socket) not in orchestrator._voice_control_bindings
    assert id(second_socket) in orchestrator._voice_control_bindings
    with pytest.raises(VoiceControlBindingError, match="binding_not_current"):
        orchestrator.validate_voice_control_binding(
            bearer=first_frame["binding"],
            subject="user-a",
            device_id=DEVICE,
            connection_generation=CONNECTION,
        )


@pytest.mark.asyncio
async def test_legacy_registration_gets_no_binding_and_send_failure_retains_none() -> None:
    orchestrator = _orchestrator()
    websocket = object()
    claims = {
        "sub": "user-a",
        "exp": int((NOW + timedelta(minutes=10)).timestamp()),
    }
    assert not await orchestrator._issue_voice_control_binding(
        websocket,
        RegisterUI(),
        claims,
    )
    orchestrator._safe_send.return_value = False
    assert not await orchestrator._issue_voice_control_binding(
        websocket,
        _registration(),
        claims,
    )
    assert id(websocket) not in orchestrator._voice_control_bindings


def test_socket_teardown_revokes_scope_and_trace_redacts_bearer(monkeypatch) -> None:
    orchestrator = _orchestrator()
    websocket = object()
    orchestrator._voice_control_bindings[id(websocket)] = (
        orchestrator._voice_binding_issuer.mint(
            subject="user-a",
            device_id=DEVICE,
            connection_generation=CONNECTION,
            credential_expires_at=NOW + timedelta(minutes=10),
        ).claims
    )
    orchestrator._clear_voice_control_binding(websocket)
    assert id(websocket) not in orchestrator._voice_control_bindings

    sink = _NonClosingStringIO()
    monkeypatch.setattr("orchestrator.orchestrator.os.path.exists", lambda _path: True)
    monkeypatch.setattr("builtins.open", lambda *_args, **_kwargs: sink)
    bearer = "never-write-this-binding-bearer-value"
    orchestrator._trace_frame(
        websocket,
        json.dumps(
            {
                "type": "voice_control_binding",
                "binding": bearer,
                "binding_id": "safe-id",
            }
        ),
        ok=False,
        error=bearer,
    )
    traced = sink.getvalue()
    assert bearer not in traced
    assert "[REDACTED]" in traced


def test_rest_validation_requires_the_exact_current_registered_binding() -> None:
    orchestrator = _orchestrator()
    websocket = object()
    issued = orchestrator._voice_binding_issuer.mint(
        subject="user-a",
        device_id=DEVICE,
        connection_generation=CONNECTION,
        credential_expires_at=NOW + timedelta(minutes=10),
    )
    orchestrator._voice_control_bindings[id(websocket)] = issued.claims

    assert orchestrator.validate_voice_control_binding(
        bearer=issued.bearer,
        subject="user-a",
        device_id=DEVICE,
        connection_generation=CONNECTION,
    ) == issued.claims

    orchestrator._clear_voice_control_binding(websocket)
    with pytest.raises(VoiceControlBindingError, match="binding_not_current"):
        orchestrator.validate_voice_control_binding(
            bearer=issued.bearer,
            subject="user-a",
            device_id=DEVICE,
            connection_generation=CONNECTION,
        )


def test_rest_validation_rejects_rotated_and_cross_scope_bearers() -> None:
    orchestrator = _orchestrator()
    websocket = object()
    first = orchestrator._voice_binding_issuer.mint(
        subject="user-a",
        device_id=DEVICE,
        connection_generation=CONNECTION,
        credential_expires_at=NOW + timedelta(minutes=10),
    )
    replacement = orchestrator._voice_binding_issuer.mint(
        subject="user-a",
        device_id=DEVICE,
        connection_generation=CONNECTION,
        credential_expires_at=NOW + timedelta(minutes=10),
    )
    orchestrator._voice_control_bindings[id(websocket)] = replacement.claims

    with pytest.raises(VoiceControlBindingError, match="binding_not_current"):
        orchestrator.validate_voice_control_binding(
            bearer=first.bearer,
            subject="user-a",
            device_id=DEVICE,
            connection_generation=CONNECTION,
        )
    with pytest.raises(VoiceControlBindingError, match="binding_scope_mismatch"):
        orchestrator.validate_voice_control_binding(
            bearer=replacement.bearer,
            subject="user-b",
            device_id=DEVICE,
            connection_generation=CONNECTION,
        )


def test_production_claim_without_keycloak_expiry_cannot_mint(monkeypatch) -> None:
    orchestrator = _orchestrator()
    monkeypatch.setenv("ASTRAL_ENV", "production")
    with pytest.raises(
        VoiceControlBindingError,
        match="credential_expiry_unavailable",
    ):
        orchestrator._voice_credential_expiry({"sub": "user-a"})
