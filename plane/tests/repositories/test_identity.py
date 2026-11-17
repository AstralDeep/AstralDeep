"""Tests for astralplane.repositories.identity: subject-scoped identity upsert and
reads, verified-external-identity atomicity and uniqueness, and nonce-replay
rejection distinct from cross-owner conflicts.
"""

from __future__ import annotations

import json

import pytest
from _support import Result, ScriptedTransaction

from astralplane.repositories import RepositoryDataError
from astralplane.repositories.identity import (
    ExternalIdentityAlreadyLinkedError,
    ExternalIdentityLinkRecord,
    ExternalIdentityNonceReplayError,
    IdentityRecord,
    IdentityRepository,
)


def identity_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "owner-1",
        "email": "owner@example.test",
        "username": "owner",
        "display_name": "Owner",
        "roles": '["member","reviewer"]',
        "last_login_at": 10,
        "created_at": 5,
        "updated_at": 10,
    }
    row.update(changes)
    return row


def test_identity_upsert_preserves_external_subject_and_returns_typed_record() -> None:
    transaction = ScriptedTransaction(one=[identity_row()])

    result = IdentityRepository().upsert_identity(
        transaction,
        owner_id="owner-1",
        observed_at=10,
        email="owner@example.test",
        username="owner",
        display_name="Owner",
        roles=("member", "reviewer", "member"),
    )

    assert result == IdentityRecord(
        owner_id="owner-1",
        email="owner@example.test",
        username="owner",
        display_name="Owner",
        roles=("member", "reviewer"),
        last_login_at=10,
        created_at=5,
        updated_at=10,
    )
    assert "COALESCE(EXCLUDED.email, users.email)" in transaction.fetch_sql()
    assert transaction.calls[0][2][0] == "owner-1"  # type: ignore[index]


def test_identity_reads_are_subject_scoped_and_admin_inventory_is_explicit() -> None:
    repository = IdentityRepository()
    assert (
        repository.get_identity(ScriptedTransaction(one=[identity_row()]), owner_id="owner-1")
        == repository.list_identities_for_administration(
            ScriptedTransaction(all_rows=[(identity_row(),)]), limit=1
        )[0]
    )
    assert repository.get_identity(ScriptedTransaction(one=[None]), owner_id="other") is None
    with pytest.raises(ValueError):
        repository.list_identities_for_administration(ScriptedTransaction(), limit=0)


def test_identity_rejects_corrupt_roles_and_invalid_claim_inputs() -> None:
    repository = IdentityRepository()
    with pytest.raises(RepositoryDataError):
        repository.get_identity(
            ScriptedTransaction(one=[identity_row(roles='{"admin":true}')]),
            owner_id="owner-1",
        )
    with pytest.raises(ValueError):
        repository.upsert_identity(ScriptedTransaction(), owner_id="", observed_at=0, roles=())
    with pytest.raises(ValueError):
        repository.upsert_identity(
            ScriptedTransaction(), owner_id="owner-1", observed_at=-1, roles=()
        )


def test_verified_external_identity_is_atomic_unique_and_preserves_preferences() -> None:
    existing = {
        "theme": {"preset": "night"},
        "verified_external_identities": {
            "orcid": {
                "subject": "old-subject",
                "issuer": "https://orcid.org",
                "verified_by_agent": "agent-1",
                "verified_at": 50,
                "recent_link_nonces": [{"nonce": "old", "used_at": 50}],
            }
        },
    }
    transaction = ScriptedTransaction(
        one=[{"locked": None}],
        all_rows=[
            (
                {"user_id": "owner-1", "preferences": json.dumps(existing)},
            )
        ],
        execute=[Result(rowcount=1)],
    )

    result = IdentityRepository().store_verified_external_identity(
        transaction,
        owner_id="owner-1",
        agent_id="agent-2",
        provider="orcid",
        subject="0000-0001-2345-6789",
        issuer="https://orcid.org",
        state_nonce="nonce-2",
        observed_at=400,
    )

    assert result == ExternalIdentityLinkRecord(
        owner_id="owner-1",
        agent_id="agent-2",
        provider="orcid",
        subject="0000-0001-2345-6789",
        issuer="https://orcid.org",
        verified_at=400,
    )
    assert "pg_advisory_xact_lock" in transaction.calls[0][1]
    assert "FOR UPDATE" in transaction.calls[1][1]
    written = json.loads(transaction.calls[2][2][1])
    assert written["theme"] == {"preset": "night"}
    entry = written["verified_external_identities"]["orcid"]
    assert entry["subject"] == "0000-0001-2345-6789"
    assert entry["recent_link_nonces"] == [{"nonce": "nonce-2", "used_at": 400}]
    assert transaction.calls[2][2][2] == 400_000


def test_external_identity_rejects_cross_owner_and_nonce_replay_distinctly() -> None:
    foreign = {
        "verified_external_identities": {
            "orcid": {
                "subject": "0000-0001-2345-6789",
                "issuer": "https://orcid.org",
                "verified_by_agent": "other-agent",
                "verified_at": 100,
            }
        }
    }
    common = {
        "owner_id": "owner-1",
        "agent_id": "agent-1",
        "provider": "orcid",
        "subject": "0000-0001-2345-6789",
        "issuer": "https://orcid.org",
        "state_nonce": "nonce-1",
        "observed_at": 200,
    }
    with pytest.raises(ExternalIdentityAlreadyLinkedError) as linked:
        IdentityRepository().store_verified_external_identity(
            ScriptedTransaction(
                one=[{"locked": None}],
                all_rows=[
                    (
                        {"user_id": "owner-2", "preferences": json.dumps(foreign)},
                    )
                ],
            ),
            **common,
        )
    assert linked.value.code == "external_identity_already_linked"

    own = dict(foreign)
    own["verified_external_identities"] = {
        "orcid": {
            **foreign["verified_external_identities"]["orcid"],
            "recent_link_nonces": [{"nonce": "nonce-1", "used_at": 150}],
        }
    }
    with pytest.raises(ExternalIdentityNonceReplayError) as replay:
        IdentityRepository().store_verified_external_identity(
            ScriptedTransaction(
                one=[{"locked": None}],
                all_rows=[
                    (
                        {"user_id": "owner-1", "preferences": json.dumps(own)},
                    )
                ],
            ),
            **common,
        )
    assert replay.value.code == "external_identity_nonce_replay"


def test_external_identity_read_list_and_corruption_are_typed() -> None:
    preferences = {
        "verified_external_identities": {
            "orcid": {
                "subject": "0000-0001-2345-6789",
                "issuer": "https://orcid.org",
                "verified_by_agent": "agent-1",
                "verified_at": 200,
            },
            "researcher": {
                "subject": "subject-2",
                "issuer": "https://issuer.invalid",
                "verified_by_agent": "agent-2",
                "verified_at": 201,
            },
        }
    }
    transaction = ScriptedTransaction(
        one=[
            {"preferences": json.dumps(preferences)},
            {"preferences": json.dumps(preferences)},
            None,
        ]
    )
    repository = IdentityRepository()
    assert repository.get_external_identity(
        transaction, owner_id="owner-1", provider="orcid"
    ).agent_id == "agent-1"  # type: ignore[union-attr]
    assert [
        item.provider
        for item in repository.list_external_identities(
            transaction, owner_id="owner-1", limit=2
        )
    ] == ["orcid", "researcher"]
    assert repository.list_external_identities(
        transaction, owner_id="missing", limit=2
    ) == ()
    with pytest.raises(RepositoryDataError):
        repository.get_external_identity(
            ScriptedTransaction(one=[{"preferences": "[]"}]),
            owner_id="owner-1",
            provider="orcid",
        )


def test_external_identity_validates_bounds_and_write_evidence() -> None:
    common = {
        "owner_id": "owner-1",
        "agent_id": "agent-1",
        "provider": "orcid",
        "subject": "0000-0001-2345-6789",
        "issuer": "https://orcid.org",
        "state_nonce": "nonce-1",
        "observed_at": 200,
    }
    for changes in (
        {"owner_id": ""},
        {"state_nonce": ""},
        {"nonce_ttl_seconds": 0},
        {"nonce_cap": 101},
    ):
        with pytest.raises(ValueError):
            IdentityRepository().store_verified_external_identity(
                ScriptedTransaction(), **{**common, **changes}
            )
    with pytest.raises(RepositoryDataError):
        IdentityRepository().store_verified_external_identity(
            ScriptedTransaction(
                one=[{"locked": None}], all_rows=[()], execute=[Result(rowcount=0)]
            ),
            **common,
        )
