"""Authenticated register/teardown integration for Feature-065 bindings."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.orchestrator import Orchestrator
from orchestrator.voice_control_binding import (
    ClientLocalBindingRegistry,
    VoiceControlBindingError,
    VoiceControlBindingIssuer,
)
from shared.protocol import VoiceLocalReady, VoiceLocalRecognitionStarted
from shared.protocol import RegisterUI


NOW = datetime(2026, 7, 31, 18, 0, tzinfo=UTC)
DEVICE = "00000000-0000-4000-8000-000000000001"
CONNECTION = "00000000-0000-4000-8000-000000000002"
SESSION = "00000000-0000-4000-8000-000000000003"
CHAT = "00000000-0000-4000-8000-000000000004"
TURN = "00000000-0000-4000-8000-000000000005"


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


def _local_session(**changes: object):
    values = {
        "session_id": SESSION,
        "user_id": "user-a",
        "device_id": DEVICE,
        "owner_connection_generation": CONNECTION,
        "control_binding_id": "00000000-0000-4000-8000-000000000006",
        "control_binding_expires_at": NOW + timedelta(minutes=4),
        "lease_expires_at": NOW + timedelta(minutes=3),
        "speech_backend": "client_local",
        "state": "active",
        "generation": 1,
        "media_grant_revision": 1,
        "visible_chat_id": CHAT,
        "chat_context_revision": 1,
        "applied_visible_chat_id": CHAT,
        "applied_chat_context_revision": 1,
        "foreground_active": True,
        "microphone_enabled": True,
        "speech_muted": False,
    }
    values.update(changes)
    return type("LocalSession", (), values)()


def _local_ready(**changes: object) -> VoiceLocalReady:
    values = {
        "device_id": DEVICE,
        "connection_generation": CONNECTION,
        "session_id": SESSION,
        "generation": 1,
        "speech_revision": 1,
        "client_sequence": 1,
    }
    values.update(changes)
    return VoiceLocalReady(**values)


def _recognition_started(**changes: object) -> VoiceLocalRecognitionStarted:
    values = {
        "device_id": DEVICE,
        "connection_generation": CONNECTION,
        "session_id": SESSION,
        "generation": 1,
        "speech_revision": 1,
        "client_turn_id": TURN,
        "chat_id": CHAT,
        "chat_context_revision": 1,
        "recognition_sequence": 1,
    }
    values.update(changes)
    return VoiceLocalRecognitionStarted(**values)


def test_local_ready_is_fenced_to_current_socket_control_and_session() -> None:
    registry = ClientLocalBindingRegistry(capacity=2)
    claims = type(
        "Claims",
        (),
        {
            "subject": "user-a",
            "device_id": DEVICE,
            "connection_generation": CONNECTION,
            "binding_id": "00000000-0000-4000-8000-000000000006",
            "expires_at": NOW + timedelta(minutes=4),
        },
    )()

    registry.authorize_ready(
        socket_id=41,
        current_socket_id=41,
        user_id="user-a",
        claims=claims,
        session=_local_session(),
        frame=_local_ready(),
        now=NOW,
    )

    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        registry.authorize_ready(
            socket_id=42,
            current_socket_id=41,
            user_id="user-a",
            claims=claims,
            session=_local_session(),
            frame=_local_ready(client_sequence=2),
            now=NOW,
        )


@pytest.mark.parametrize(
    "session_change, frame_change",
    [
        ({"user_id": "user-b"}, {}),
        ({"device_id": "00000000-0000-4000-8000-000000000099"}, {}),
        ({"foreground_active": False}, {}),
        ({"microphone_enabled": False}, {}),
        ({"speech_muted": True}, {}),
        ({"chat_context_revision": 2}, {}),
        ({}, {"speech_revision": 2}),
        ({}, {"chat_id": "00000000-0000-4000-8000-000000000099"}),
    ],
)
def test_local_start_rejects_stale_or_ineligible_authority(
    session_change: dict[str, object],
    frame_change: dict[str, object],
) -> None:
    registry = ClientLocalBindingRegistry()
    claims = type(
        "Claims",
        (),
        {
            "subject": "user-a",
            "device_id": DEVICE,
            "connection_generation": CONNECTION,
            "binding_id": "00000000-0000-4000-8000-000000000006",
            "expires_at": NOW + timedelta(minutes=4),
        },
    )()

    with pytest.raises(VoiceControlBindingError):
        registry.authorize_recognition_start(
            socket_id=41,
            current_socket_id=41,
            user_id="user-a",
            claims=claims,
            session=_local_session(**session_change),
            frame=_recognition_started(**frame_change),
            now=NOW,
        )


def test_local_turn_binding_expires_within_two_minutes_and_is_bounded() -> None:
    registry = ClientLocalBindingRegistry(capacity=1)
    claims = type(
        "Claims",
        (),
        {
            "subject": "user-a",
            "device_id": DEVICE,
            "connection_generation": CONNECTION,
            "binding_id": "00000000-0000-4000-8000-000000000006",
            "expires_at": NOW + timedelta(minutes=4),
        },
    )()
    session = _local_session()
    frame = _recognition_started()
    turn = type(
        "Turn",
        (),
        {
            "turn_id": "00000000-0000-4000-8000-000000000007",
            "submission_id": "00000000-0000-4000-8000-000000000008",
            "request_generation": "00000000-0000-4000-8000-000000000009",
        },
    )()

    authority = registry.bind_turn(
        socket_id=41,
        current_socket_id=41,
        user_id="user-a",
        claims=claims,
        session=session,
        frame=frame,
        turn=turn,
        now=NOW,
    )
    assert authority.expires_at == NOW + timedelta(minutes=2)

    with pytest.raises(VoiceControlBindingError, match="capacity_exhausted"):
        registry.bind_turn(
            socket_id=41,
            current_socket_id=41,
            user_id="user-a",
            claims=claims,
            session=session,
            frame=_recognition_started(
                client_turn_id="00000000-0000-4000-8000-000000000010",
                recognition_sequence=2,
            ),
            turn=turn,
            now=NOW,
        )


def test_local_registry_replay_validation_and_cleanup_are_exact() -> None:
    with pytest.raises(ValueError, match="invalid_local_binding_capacity"):
        ClientLocalBindingRegistry(capacity=0)

    registry = ClientLocalBindingRegistry()
    claims = type(
        "Claims",
        (),
        {
            "subject": "user-a",
            "device_id": DEVICE,
            "connection_generation": CONNECTION,
            "binding_id": "00000000-0000-4000-8000-000000000006",
            "expires_at": NOW + timedelta(minutes=4),
        },
    )()
    session = _local_session()
    frame = _recognition_started()
    turn = type(
        "Turn",
        (),
        {
            "turn_id": "00000000-0000-4000-8000-000000000007",
            "submission_id": "00000000-0000-4000-8000-000000000008",
            "request_generation": "00000000-0000-4000-8000-000000000009",
        },
    )()
    authority = registry.bind_turn(
        socket_id=41,
        current_socket_id=41,
        user_id="user-a",
        claims=claims,
        session=session,
        frame=frame,
        turn=turn,
        now=NOW,
    )
    bound_frame = SimpleNamespace(
        **frame.__dict__,
        turn_id=turn.turn_id,
        submission_id=turn.submission_id,
        request_generation=turn.request_generation,
        validate=frame.validate,
    )
    assert registry.bind_turn(
        socket_id=41,
        current_socket_id=41,
        user_id="user-a",
        claims=claims,
        session=session,
        frame=bound_frame,
        turn=turn,
        now=NOW,
    ) is authority
    assert registry.verify_turn_frame(
        socket_id=41,
        current_socket_id=41,
        user_id="user-a",
        frame=bound_frame,
        now=NOW,
    ) is authority

    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        registry.bind_turn(
            socket_id=41,
            current_socket_id=41,
            user_id="user-a",
            claims=claims,
            session=session,
            frame=_recognition_started(recognition_sequence=2),
            turn=turn,
            now=NOW,
        )
    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        registry.authorize_ready(
            socket_id=41,
            current_socket_id=41,
            user_id="user-a",
            claims=claims,
            session=session,
            frame=_local_ready(client_sequence=1),
            now=NOW,
        )
    final_frame = SimpleNamespace(
        **bound_frame.__dict__,
        text="hello",
        text_digest_sha256="2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
    )
    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        registry.verify_final(
            socket_id=41,
            current_socket_id=42,
            user_id="user-a",
            frame=final_frame,
            now=NOW,
        )

    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        registry.verify_turn_frame(
            socket_id=41,
            current_socket_id=42,
            user_id="user-a",
            frame=bound_frame,
            now=NOW,
        )
    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        registry.authorize_ready(
            socket_id=41,
            current_socket_id=41,
            user_id="user-a",
            claims=claims,
            session=session,
            frame=SimpleNamespace(validate=lambda: (_ for _ in ()).throw(ValueError())),
            now=NOW,
        )

    registry.clear_connection(
        user_id="user-a",
        device_id=DEVICE,
        connection_generation=CONNECTION,
    )
    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        registry.get_turn(
            user_id="user-a",
            client_turn_id=TURN,
            now=NOW,
        )
    registry.release_turn(user_id="user-a", client_turn_id=TURN)


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
