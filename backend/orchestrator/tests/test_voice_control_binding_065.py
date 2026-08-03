"""Feature-065 short-lived UI control-binding tests."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.voice_control_binding import (
    VoiceControlBindingError,
    VoiceControlBindingIssuer,
)


NOW = datetime(2026, 7, 31, 18, 0, tzinfo=UTC)
DEVICE = "00000000-0000-4000-8000-000000000001"
CONNECTION = "00000000-0000-4000-8000-000000000002"


def _issuer(*, at: datetime = NOW) -> VoiceControlBindingIssuer:
    return VoiceControlBindingIssuer(b"v" * 32, clock=lambda: at)


def test_mint_and_verify_are_subject_device_connection_and_credential_bound() -> None:
    issued = _issuer().mint(
        subject="user-a",
        device_id=DEVICE,
        connection_generation=CONNECTION,
        credential_expires_at=NOW + timedelta(minutes=3),
    )

    assert issued.claims.expires_at == NOW + timedelta(minutes=3)
    assert 32 <= len(issued.bearer) <= 512
    assert issued.bearer not in repr(issued)
    assert "v" * 32 not in repr(_issuer())
    verified = _issuer().verify(
        issued.bearer,
        expected_subject="user-a",
        expected_device_id=DEVICE,
        expected_connection_generation=CONNECTION,
        expected_binding_id=issued.claims.binding_id,
        expected_expires_at=issued.claims.expires_at,
    )
    assert verified == issued.claims


def test_fractional_clock_claims_round_trip_at_token_precision() -> None:
    fractional_now = NOW.replace(microsecond=987_654)
    fractional_expiry = (NOW + timedelta(minutes=3)).replace(
        microsecond=456_789
    )
    issuer = _issuer(at=fractional_now)

    issued = issuer.mint(
        subject="user-a",
        device_id=DEVICE,
        connection_generation=CONNECTION,
        credential_expires_at=fractional_expiry,
    )
    verified = issuer.verify(
        issued.bearer,
        expected_subject="user-a",
        expected_device_id=DEVICE,
        expected_connection_generation=CONNECTION,
    )

    assert issued.claims.issued_at == NOW
    assert issued.claims.expires_at == NOW + timedelta(minutes=3)
    assert verified == issued.claims


def test_lifetime_is_capped_at_ten_minutes() -> None:
    issued = _issuer().mint(
        subject="user-a",
        device_id=DEVICE,
        connection_generation=CONNECTION,
        credential_expires_at=NOW + timedelta(hours=1),
    )
    assert issued.claims.expires_at == NOW + timedelta(minutes=10)


@pytest.mark.parametrize(
    ("change", "code"),
    (
        ({"expected_subject": "user-b"}, "binding_scope_mismatch"),
        (
            {"expected_device_id": "00000000-0000-4000-8000-000000000003"},
            "binding_scope_mismatch",
        ),
        (
            {
                "expected_connection_generation": (
                    "00000000-0000-4000-8000-000000000004"
                )
            },
            "binding_scope_mismatch",
        ),
        (
            {"expected_binding_id": "00000000-0000-4000-8000-000000000005"},
            "binding_scope_mismatch",
        ),
    ),
)
def test_scope_transplant_is_refused(change: dict[str, object], code: str) -> None:
    issued = _issuer().mint(
        subject="user-a",
        device_id=DEVICE,
        connection_generation=CONNECTION,
        credential_expires_at=NOW + timedelta(minutes=10),
    )
    expected: dict[str, object] = {
        "expected_subject": "user-a",
        "expected_device_id": DEVICE,
        "expected_connection_generation": CONNECTION,
        "expected_binding_id": issued.claims.binding_id,
    }
    expected.update(change)
    with pytest.raises(VoiceControlBindingError, match=code):
        _issuer().verify(issued.bearer, **expected)  # type: ignore[arg-type]


def test_altered_bad_format_and_expired_bearers_are_content_free_failures() -> None:
    issued = _issuer().mint(
        subject="user-a",
        device_id=DEVICE,
        connection_generation=CONNECTION,
        credential_expires_at=NOW + timedelta(minutes=10),
    )
    altered = issued.bearer[:-1] + ("A" if issued.bearer[-1] != "A" else "B")
    expected = {
        "expected_subject": "user-a",
        "expected_device_id": DEVICE,
        "expected_connection_generation": CONNECTION,
    }
    for bearer in ("not-a-token", altered, "x" * 513):
        with pytest.raises(VoiceControlBindingError) as caught:
            _issuer().verify(bearer, **expected)
        assert issued.bearer not in str(caught.value)

    with pytest.raises(VoiceControlBindingError, match="binding_expired"):
        _issuer(at=NOW + timedelta(minutes=11)).verify(
            issued.bearer,
            **expected,
        )


def test_duplicate_or_extra_claims_cannot_survive_even_with_valid_signature() -> None:
    issuer = _issuer()
    issued = issuer.mint(
        subject="user-a",
        device_id=DEVICE,
        connection_generation=CONNECTION,
        credential_expires_at=NOW + timedelta(minutes=10),
    )
    _version, encoded, _signature = issued.bearer.split(".")
    payload = json.loads(
        base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    )
    payload["admin"] = True
    # The public verifier must reject the shape. A mismatched signature is an
    # equally safe rejection and proves untrusted claims cannot be injected.
    replacement = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    forged = f"v1.{replacement}.{issued.bearer.rsplit('.', 1)[1]}"
    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        issuer.verify(
            forged,
            expected_subject="user-a",
            expected_device_id=DEVICE,
            expected_connection_generation=CONNECTION,
        )


def test_environment_configuration_fails_closed_outside_development() -> None:
    with pytest.raises(VoiceControlBindingError, match="missing_binding_secret"):
        VoiceControlBindingIssuer.from_environ({"ASTRAL_ENV": "production"})
    with pytest.raises(VoiceControlBindingError, match="weak_binding_secret"):
        VoiceControlBindingIssuer.from_environ(
            {
                "ASTRAL_ENV": "production",
                "VOICE_UI_BINDING_SECRET": "short",
            }
        )
    development = VoiceControlBindingIssuer.from_environ(
        {"ASTRAL_ENV": "development"},
        clock=lambda: NOW,
    )
    assert repr(development) == "VoiceControlBindingIssuer(secret=<redacted>)"


def test_expired_credential_cannot_mint_a_binding() -> None:
    with pytest.raises(VoiceControlBindingError, match="credential_expired"):
        _issuer().mint(
            subject="user-a",
            device_id=DEVICE,
            connection_generation=CONNECTION,
            credential_expires_at=NOW,
        )
