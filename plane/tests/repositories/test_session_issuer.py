"""Tests for astralplane.repositories.history: issuing-metadata (issuer/client) fields
are exact, paired, and bound to a session without themselves granting any authority.
"""

import hashlib
import json
from dataclasses import replace
from datetime import UTC

import pytest

from astralplane.repositories import RepositoryDataError, RepositoryValidationError
from astralplane.repositories.history import SessionRecord, SessionRepository
from tests.repositories.test_history import FakeTransaction, session_row

ISSUER = "https://iam.example.test/realms/Astral"
CLIENT = "astral-mobile"
INCARNATION = "12345678-1234-4234-8234-123456789abc"


def record(**changes):
    return replace(
        SessionRecord(
            "session", "owner", "cipher-a", "cipher-r", 10, 100, 20, False, 5, INCARNATION
        ),
        **changes,
    )


def test_legacy_hash_and_positional_session_contract_remain_exact():
    fence = SessionRepository.execution_fence(record())
    assert fence.version == 2
    assert fence.issuing_issuer is None and fence.issuing_client_id is None
    assert fence.encrypted_state_binding == hashlib.sha256(b'["cipher-a","cipher-r"]').hexdigest()


def test_bound_pair_changes_fence_and_preserves_exact_unicode_strings():
    original = record(issuing_issuer=ISSUER + "/\u00e9", issuing_client_id=CLIENT)
    fence = SessionRepository.execution_fence(original)
    assert fence.version == 2
    assert fence.issuing_issuer == original.issuing_issuer
    assert fence.issuing_client_id == CLIENT
    expected = [
        "cipher-a",
        "cipher-r",
        {"issuing_issuer": original.issuing_issuer, "issuing_client_id": CLIENT},
    ]
    assert (
        fence.encrypted_state_binding
        == hashlib.sha256(
            json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    for changed in (
        record(),
        replace(original, issuing_client_id="astral-desktop"),
        replace(original, issuing_issuer=ISSUER + "/e\u0301"),
    ):
        assert SessionRepository.execution_fence(changed) != fence
        assert (
            SessionRepository.execution_fence(changed).encrypted_state_binding
            != fence.encrypted_state_binding
        )


@pytest.mark.parametrize(
    "field,other,maximum",
    [
        ("issuing_issuer", "issuing_client_id", 2048),
        ("issuing_client_id", "issuing_issuer", 256),
    ],
)
@pytest.mark.parametrize(
    "invalid",
    [
        None,
        "",
        " ",
        True,
        1,
        [],
        {},
        " leading",
        "trailing ",
        "line\nbreak",
        "nul\x00",
        "del\x7f",
        "c1\x85",
        "lone\ud800",
    ],
)
def test_invalid_pair_fails_before_any_database_call(field, other, maximum, invalid):
    value = record(**{field: invalid, other: "valid"})
    with pytest.raises(RepositoryValidationError):
        SessionRepository().put(None, value)
    with pytest.raises(RepositoryValidationError):
        SessionRepository.execution_fence(value)


@pytest.mark.parametrize("field,maximum", [("issuing_issuer", 2048), ("issuing_client_id", 256)])
def test_exact_field_bounds_and_no_text_truncation(field, maximum):
    value = record(issuing_issuer=ISSUER, issuing_client_id=CLIENT)
    accepted = replace(value, **{field: "x" * maximum})
    assert getattr(SessionRepository.execution_fence(accepted), field) == "x" * maximum
    with pytest.raises(RepositoryValidationError):
        SessionRepository.execution_fence(replace(value, **{field: "x" * (maximum + 1)}))


@pytest.mark.parametrize(
    "changes",
    [
        {"issuing_issuer": ISSUER},
        {"issuing_client_id": CLIENT},
        {"issuing_issuer": True, "issuing_client_id": CLIENT},
        {"issuing_issuer": ISSUER, "issuing_client_id": "\ud800"},
    ],
)
def test_malformed_stored_pair_is_data_error(changes):
    query = FakeTransaction()
    query.fetch_one_results.append(
        session_row(**{"issuing_issuer": None, "issuing_client_id": None, **changes})
    )
    with pytest.raises(RepositoryDataError):
        SessionRepository().get(query, owner_id="owner-1", session_id="session-1")


def test_missing_session_identity_column_is_not_adopted_as_legacy():
    query = FakeTransaction()
    row = session_row()
    del row["issuing_issuer"]
    query.fetch_one_results.append(row)
    with pytest.raises(RepositoryDataError):
        SessionRepository().get(query, owner_id="owner-1", session_id="session-1")


def test_malformed_fence_pair_refuses_before_any_database_call():
    from datetime import datetime, timedelta

    from astralplane.repositories.history import SessionExecutionObservation

    fence = replace(SessionRepository.execution_fence(record()), issuing_issuer=ISSUER)
    now = datetime.now(UTC)
    with pytest.raises(RepositoryValidationError):
        SessionRepository().assert_current_execution(
            None, observation=SessionExecutionObservation(fence, now, now + timedelta(seconds=15))
        )
