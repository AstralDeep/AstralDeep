"""Purpose-bound one-time Watch bridge ticket tests for Feature 065."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from shared.watch_ticket import (
    WatchTicketError,
    derive_watch_nonce,
    issue_watch_ticket,
    verify_watch_ticket,
    watch_participant_identity,
)


NOW = datetime(2026, 8, 1, 14, 0, tzinfo=UTC)
SECRET = b"watch-ticket-test-secret-that-is-long-enough"
USER_ID = "owner@example.invalid"
SESSION_ID = "00000000-0000-4000-8000-000000000101"
DEVICE_ID = "00000000-0000-4000-8000-000000000102"
CONNECTION_ID = "00000000-0000-4000-8000-000000000103"
REFRESH_ID = "00000000-0000-4000-8000-000000000104"
WORKER_ID = "voice-worker-a"


def _nonce() -> bytes:
    return derive_watch_nonce(
        SECRET,
        user_id=USER_ID,
        session_key=REFRESH_ID,
        generation=2,
        media_grant_revision=3,
        device_id=DEVICE_ID,
        connection_generation=CONNECTION_ID,
    )


def _ticket() -> str:
    return issue_watch_ticket(
        SECRET,
        user_id=USER_ID,
        session_id=SESSION_ID,
        generation=2,
        media_grant_revision=3,
        worker_identity=WORKER_ID,
        device_id=DEVICE_ID,
        connection_generation=CONNECTION_ID,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=1),
        nonce=_nonce(),
    )


def _tampered_ticket() -> str:
    version, payload, signature = _ticket().split(".")
    replacement = "A" if signature[0] != "A" else "B"
    return ".".join((version, payload, replacement + signature[1:]))


def test_ticket_is_deterministically_remintable_and_exactly_scoped() -> None:
    assert _ticket() == _ticket()
    claims = verify_watch_ticket(
        _ticket(),
        SECRET,
        now=NOW + timedelta(seconds=30),
        expected_worker_identity=WORKER_ID,
    )
    assert claims.session_id == SESSION_ID
    assert claims.generation == 2
    assert claims.media_grant_revision == 3
    assert claims.device_id == DEVICE_ID
    assert claims.connection_generation == CONNECTION_ID
    assert claims.nonce == _nonce()
    assert watch_participant_identity(claims.nonce).startswith("watch-")
    assert _ticket() not in repr(claims)
    assert claims.nonce.hex() not in repr(claims)


@pytest.mark.parametrize(
    ("ticket", "now", "worker", "code"),
    (
        (_tampered_ticket, NOW, WORKER_ID, "invalid_ticket"),
        (
            _ticket,
            NOW + timedelta(minutes=1),
            WORKER_ID,
            "ticket_expired",
        ),
        (_ticket, NOW, "voice-worker-b", "wrong_worker"),
    ),
)
def test_ticket_rejects_tampering_expiry_and_wrong_worker(
    ticket,
    now: datetime,
    worker: str,
    code: str,
) -> None:
    with pytest.raises(WatchTicketError, match=code):
        verify_watch_ticket(
            ticket(),
            SECRET,
            now=now,
            expected_worker_identity=worker,
        )


def test_nonce_is_bound_to_device_connection_and_revision() -> None:
    baseline = _nonce()
    for changed in (
        {"device_id": "00000000-0000-4000-8000-000000000105"},
        {"connection_generation": "00000000-0000-4000-8000-000000000106"},
        {"media_grant_revision": 4},
    ):
        fields = {
            "user_id": USER_ID,
            "session_key": REFRESH_ID,
            "generation": 2,
            "media_grant_revision": 3,
            "device_id": DEVICE_ID,
            "connection_generation": CONNECTION_ID,
        }
        fields.update(changed)
        assert derive_watch_nonce(SECRET, **fields) != baseline
