"""Scripted capability-publication failure paths prove malformed stored rows and races
that lose their compare-and-set fence surface as typed errors instead of silent success."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from astralplane.repositories import RepositoryConflictError, RepositoryDataError
from astralplane.repositories.capability_publications import (
    CapabilityPublicationRepository,
    CapabilityPublicationState,
)
from tests.repositories._support import ScriptedTransaction

AT = datetime(2026, 10, 10, tzinfo=UTC)


def _row(**changes):
    values = {
        "owner_id": "owner",
        "capability_id": "capability",
        "audience_id": "audience",
        "capability_revision": "revision",
        "metadata_ciphertext": "ciphertext",
        "metadata_digest": "a" * 64,
        "publication_state": "published",
        "state_revision": 1,
        "published_at": AT,
        "expires_at": AT + timedelta(hours=1),
        "ended_at": None,
        "end_reason": None,
    }
    return values | changes


def _publish(tx, **changes):
    values = {
        "owner_id": "owner",
        "capability_id": "capability",
        "audience_id": "audience",
        "capability_revision": "revision",
        "metadata_ciphertext": "ciphertext",
        "metadata_digest": "a" * 64,
        "published_at": AT,
        "expires_at": AT + timedelta(hours=1),
        "expected_revision": 0,
    }
    return CapabilityPublicationRepository().publish(tx, **(values | changes))


def test_malformed_stored_rows_fail_closed() -> None:
    repo = CapabilityPublicationRepository()
    cases = (
        _row(publication_state="unknown"),
        _row(state_revision=0),
        _row(published_at=datetime(2026, 10, 10)),
        _row(owner_id=""),
        _row(end_reason=7),
        {"owner_id": "owner"},
    )
    for stored in cases:
        tx = ScriptedTransaction(one=[stored])
        with pytest.raises(RepositoryDataError):
            repo.get(
                tx,
                owner_id="owner",
                capability_id="capability",
                audience_id="audience",
            )

    missing = _row()
    missing.pop("metadata_digest")
    with pytest.raises(RepositoryDataError):
        repo.get(
            ScriptedTransaction(one=[missing]),
            owner_id="owner",
            capability_id="capability",
            audience_id="audience",
        )


def test_lost_insert_row_is_a_data_error() -> None:
    tx = ScriptedTransaction(one=[None, None, None])
    with pytest.raises(RepositoryDataError):
        _publish(tx)
    assert len(tx.calls) == 3


def test_absent_and_already_published_revision_conflicts() -> None:
    with pytest.raises(RepositoryConflictError):
        _publish(ScriptedTransaction(one=[None]), expected_revision=1)
    with pytest.raises(RepositoryConflictError):
        _publish(
            ScriptedTransaction(one=[_row()]),
            expected_revision=1,
            capability_revision="revision-two",
        )
    repo = CapabilityPublicationRepository()
    assert repo.get(
        ScriptedTransaction(one=[_row()]),
        owner_id="owner",
        capability_id="capability",
        audience_id="audience",
        for_update=True,
    ).state_revision == 1


def test_rejected_transition_fences_are_typed_conflicts() -> None:
    repo = CapabilityPublicationRepository()
    with pytest.raises(RepositoryConflictError):
        repo.withdraw(
            ScriptedTransaction(one=[_row(), None]),
            owner_id="owner",
            capability_id="capability",
            audience_id="audience",
            expected_revision=1,
            at=AT + timedelta(minutes=1),
        )
    with pytest.raises(RepositoryConflictError):
        repo.expire(
            ScriptedTransaction(one=[_row(), None]),
            owner_id="owner",
            capability_id="capability",
            audience_id="audience",
            expected_revision=1,
            at=AT + timedelta(hours=2),
        )
    with pytest.raises(RepositoryConflictError):
        repo.publish(
            ScriptedTransaction(
                one=[_row(publication_state="withdrawn", state_revision=2), None]
            ),
            owner_id="owner",
            capability_id="capability",
            audience_id="audience",
            capability_revision="revision-two",
            metadata_ciphertext="ciphertext-two",
            metadata_digest="b" * 64,
            published_at=AT + timedelta(hours=2),
            expires_at=AT + timedelta(hours=3),
            expected_revision=2,
        )
    with pytest.raises(RepositoryConflictError):
        repo.withdraw(
            ScriptedTransaction(one=[_row(state_revision=2**63 - 1)]),
            owner_id="owner",
            capability_id="capability",
            audience_id="audience",
            expected_revision=2**63 - 1,
            at=AT + timedelta(minutes=1),
        )
    with pytest.raises(RepositoryConflictError):
        repo.expire(
            ScriptedTransaction(
                one=[
                    _row(
                        publication_state="withdrawn",
                        state_revision=2,
                        ended_at=AT + timedelta(minutes=1),
                    )
                ]
            ),
            owner_id="owner",
            capability_id="capability",
            audience_id="audience",
            expected_revision=2,
            at=AT + timedelta(hours=2),
        )


def test_replay_identity_and_terminal_shape_are_checked() -> None:
    repo = CapabilityPublicationRepository()
    assert repo.get(
        ScriptedTransaction(
            one=[
                _row(
                    publication_state="withdrawn",
                    state_revision=2,
                    ended_at=AT + timedelta(minutes=1),
                    end_reason="",
                )
            ]
        ),
        owner_id="owner",
        capability_id="capability",
        audience_id="audience",
    ).state is CapabilityPublicationState.WITHDRAWN
    with pytest.raises(RepositoryConflictError):
        repo.withdraw(
            ScriptedTransaction(
                one=[
                    _row(
                        publication_state="withdrawn",
                        state_revision=2,
                        ended_at=AT + timedelta(minutes=1),
                        end_reason="first",
                    )
                ]
            ),
            owner_id="owner",
            capability_id="capability",
            audience_id="audience",
            expected_revision=1,
            at=AT + timedelta(minutes=1),
            reason="changed",
        )