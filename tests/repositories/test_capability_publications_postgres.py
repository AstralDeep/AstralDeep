"""Real PostgreSQL tests for neutral capability-publication records: owner scope,
publish/withdraw/expire fencing, expiry-aware reads, replay, restart, concurrency,
and caller rollback. Synthetic ciphertext and metadata digests are non-PHI."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone

import pytest
from test_assignments_postgres import database as database
from test_assignments_postgres import (
    independent_database,
    parallel_transactions,
    standalone_database,
)

import astralplane
from astralplane.repositories import (
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.capability_publications import (
    CapabilityPublicationConflictError,
    CapabilityPublicationNotExpiredError,
    CapabilityPublicationRepository,
    CapabilityPublicationState,
)

AT = datetime(2026, 10, 10, tzinfo=UTC)
OWNER = "publication-owner"
CAPABILITY = "capability-one"
AUDIENCE = "audience-one"
CIPHERTEXT = "synthetic-opaque-ciphertext"
DIGEST = "a" * 64


@pytest.fixture(autouse=True)
def empty_publication_inventory(database):
    with database.transaction() as tx:
        tx.execute("DELETE FROM capability_publication")


@contextmanager
def isolated():
    generator = standalone_database()
    db = next(generator)
    try:
        yield db
    finally:
        generator.close()


def publish(tx, repo=None, **changes):
    values = dict(
        owner_id=OWNER,
        capability_id=CAPABILITY,
        audience_id=AUDIENCE,
        capability_revision="revision-one",
        metadata_ciphertext=CIPHERTEXT,
        metadata_digest=DIGEST,
        published_at=AT,
        expires_at=AT + timedelta(hours=1),
        expected_revision=0,
    )
    return (repo or CapabilityPublicationRepository()).publish(tx, **(values | changes))


def withdraw(tx, repo=None, **changes):
    values = dict(
        owner_id=OWNER,
        capability_id=CAPABILITY,
        audience_id=AUDIENCE,
        expected_revision=1,
        at=AT + timedelta(minutes=5),
        reason="owner withdrew the capability",
    )
    return (repo or CapabilityPublicationRepository()).withdraw(tx, **(values | changes))


def expire(tx, repo=None, **changes):
    values = dict(
        owner_id=OWNER,
        capability_id=CAPABILITY,
        audience_id=AUDIENCE,
        expected_revision=1,
        at=AT + timedelta(hours=2),
    )
    return (repo or CapabilityPublicationRepository()).expire(tx, **(values | changes))


def test_owner_scope_and_active_reads_never_cross_owners() -> None:
    with isolated() as db:
        repo = astralplane.create_capability_publication_repository()
        with db.transaction() as tx:
            assert repo.get(
                tx,
                owner_id=OWNER,
                capability_id=CAPABILITY,
                audience_id=AUDIENCE,
            ) is None
            published = publish(tx, repo)
            assert published.state is CapabilityPublicationState.PUBLISHED
            assert repo.get(tx, owner_id=OWNER, capability_id=CAPABILITY, audience_id=AUDIENCE) == (
                published
            )
            assert repo.get(
                tx, owner_id="foreign-owner", capability_id=CAPABILITY, audience_id=AUDIENCE
            ) is None
            assert repo.get_active(
                tx,
                owner_id="foreign-owner",
                capability_id=CAPABILITY,
                audience_id=AUDIENCE,
                at=AT + timedelta(minutes=1),
            ) is None
            assert repo.list_active(
                tx, owner_id="foreign-owner", at=AT + timedelta(minutes=1), limit=10
            ) == ()
            with pytest.raises(RepositoryNotFoundError):
                withdraw(tx, repo, owner_id="foreign-owner")
            with pytest.raises(RepositoryNotFoundError):
                expire(tx, repo, owner_id="foreign-owner")
            assert (
                repo.get(tx, owner_id=OWNER, capability_id=CAPABILITY, audience_id=AUDIENCE)
                == published
            )


def test_exact_publish_replay_is_idempotent_and_changed_replay_conflicts() -> None:
    with isolated() as db:
        repo = CapabilityPublicationRepository()
        with db.transaction() as tx:
            published = publish(tx, repo)
            assert publish(tx, repo) == published
            assert publish(tx, repo, expected_revision=1) == published
            for change in (
                {"capability_revision": "revision-two"},
                {"metadata_ciphertext": "different ciphertext"},
                {"metadata_digest": "b" * 64},
                {"expires_at": AT + timedelta(hours=2)},
            ):
                with pytest.raises(CapabilityPublicationConflictError):
                    publish(tx, repo, **change)
            assert (
                repo.get(tx, owner_id=OWNER, capability_id=CAPABILITY, audience_id=AUDIENCE)
                == published
            )


def test_stale_withdraw_and_concurrent_first_publish_are_fenced() -> None:
    with isolated() as db:
        repo = CapabilityPublicationRepository()
        results = parallel_transactions(
            db,
            (lambda tx: publish(tx, repo), lambda tx: publish(tx, repo)),
        )
        assert results[0] == results[1]
        assert results[0].state_revision == 1
        with db.transaction() as tx:
            with pytest.raises(CapabilityPublicationConflictError):
                withdraw(tx, repo, expected_revision=0)
            withdrawn = withdraw(tx, repo)
            assert withdrawn.state is CapabilityPublicationState.WITHDRAWN
            assert withdraw(tx, repo) == withdrawn
            with pytest.raises(CapabilityPublicationConflictError):
                withdraw(tx, repo, reason="changed replay")
            with pytest.raises(CapabilityPublicationConflictError):
                withdraw(tx, repo, expected_revision=3)
            assert repo.get_active(
                tx,
                owner_id=OWNER,
                capability_id=CAPABILITY,
                audience_id=AUDIENCE,
                at=AT + timedelta(minutes=10),
            ) is None


def test_caller_failure_rolls_back_and_restart_reads_durable_state() -> None:
    with isolated() as db:
        with pytest.raises(RuntimeError, match="audit failure"), db.transaction() as tx:
            publish(tx)
            raise RuntimeError("audit failure")
        with db.transaction() as tx:
            assert CapabilityPublicationRepository().get(
                tx, owner_id=OWNER, capability_id=CAPABILITY, audience_id=AUDIENCE
            ) is None
            committed = publish(tx, CapabilityPublicationRepository())
        with db.transaction() as tx:
            fresh = CapabilityPublicationRepository()
            assert fresh.get(
                tx, owner_id=OWNER, capability_id=CAPABILITY, audience_id=AUDIENCE
            ) == committed
            assert fresh.get_active(
                tx,
                owner_id=OWNER,
                capability_id=CAPABILITY,
                audience_id=AUDIENCE,
                at=AT + timedelta(minutes=1),
            ) == committed


def test_expiry_reads_and_atomic_expiry_transition(database):
    repo = CapabilityPublicationRepository()
    with database.transaction() as tx:
        published = publish(tx, repo)
        assert repo.get_active(
            tx,
            owner_id=OWNER,
            capability_id=CAPABILITY,
            audience_id=AUDIENCE,
            at=AT,
        ) == published
        assert repo.get_active(
            tx,
            owner_id=OWNER,
            capability_id=CAPABILITY,
            audience_id=AUDIENCE,
            at=published.expires_at,
        ) is None
        with pytest.raises(CapabilityPublicationNotExpiredError):
            expire(tx, repo, at=published.expires_at - timedelta(seconds=1))
        expired = expire(tx, repo, at=published.expires_at)
        assert expired.state is CapabilityPublicationState.EXPIRED
        assert expired.state_revision == 2
        assert expire(tx, repo, at=published.expires_at) == expired
        with pytest.raises(CapabilityPublicationConflictError):
            expire(tx, repo, at=published.expires_at + timedelta(seconds=1))
        with pytest.raises(CapabilityPublicationConflictError):
            withdraw(tx, repo, expected_revision=2)
        assert repo.get_active(
            tx,
            owner_id=OWNER,
            capability_id=CAPABILITY,
            audience_id=AUDIENCE,
            at=published.expires_at,
        ) is None


def test_republish_after_withdrawal_and_expiry_updates_identity(database):
    repo = CapabilityPublicationRepository()
    with database.transaction() as tx:
        published = publish(tx, repo)
        withdrawn = withdraw(tx, repo)
        replacement = publish(
            tx,
            repo,
            expected_revision=withdrawn.state_revision,
            capability_revision="revision-two",
            metadata_ciphertext="replacement ciphertext",
            metadata_digest="b" * 64,
            published_at=AT + timedelta(hours=3),
            expires_at=AT + timedelta(hours=4),
        )
        assert replacement.state is CapabilityPublicationState.PUBLISHED
        assert replacement.state_revision == 3
        assert replacement.capability_revision == "revision-two"
        assert replacement.ended_at is None
        assert repo.get_active(
            tx,
            owner_id=OWNER,
            capability_id=CAPABILITY,
            audience_id=AUDIENCE,
            at=AT + timedelta(hours=2),
        ) is None
        assert repo.get_active(
            tx,
            owner_id=OWNER,
            capability_id=CAPABILITY,
            audience_id=AUDIENCE,
            at=AT + timedelta(hours=3, minutes=30),
        ) == replacement
        assert repo.get(tx, owner_id=OWNER, capability_id=CAPABILITY, audience_id=AUDIENCE) != (
            published
        )


def test_bounded_active_inventory_is_deterministically_ordered(database):
    repo = CapabilityPublicationRepository()
    with database.transaction() as tx:
        for capability, audience in (
            ("capability-b", "audience-2"),
            ("capability-a", "audience-2"),
            ("capability-a", "audience-1"),
        ):
            publish(tx, repo, capability_id=capability, audience_id=audience)
        publish(
            tx,
            repo,
            capability_id="capability-c",
            audience_id="audience-1",
            published_at=AT + timedelta(hours=2),
            expires_at=AT + timedelta(hours=3),
        )
        rows = repo.list_active(tx, owner_id=OWNER, at=AT + timedelta(minutes=1), limit=2)
        assert [(row.capability_id, row.audience_id) for row in rows] == [
            ("capability-a", "audience-1"),
            ("capability-a", "audience-2"),
        ]
        with pytest.raises(RepositoryValidationError):
            repo.list_active(tx, owner_id=OWNER, at=AT, limit=0)
        with pytest.raises(RepositoryValidationError):
            repo.list_active(tx, owner_id=OWNER, at=AT, limit=201)


def test_lock_reads_hold_the_owner_row_to_caller_commit(database):
    from tests.fixtures.database_errors import LOCK_ERRORS

    repo = CapabilityPublicationRepository()
    with database.transaction() as tx:
        publish(tx, repo)
    with database.transaction() as tx:
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
        assert repo.get_active(
            tx,
            owner_id=OWNER,
            capability_id=CAPABILITY,
            audience_id=AUDIENCE,
            at=AT + timedelta(minutes=1),
            for_update=True,
        ).state_revision == 1
        with (
            independent_database(schema) as second,
            pytest.raises(LOCK_ERRORS),
            second.transaction() as other,
        ):
            other.execute("SET LOCAL lock_timeout = '50ms'")
            withdraw(other, repo)
    with database.transaction() as tx:
        assert withdraw(tx, repo).state is CapabilityPublicationState.WITHDRAWN


def test_withdrawal_before_publication_and_empty_reason(database):
    repo = CapabilityPublicationRepository()
    with database.transaction() as tx:
        publish(tx, repo, published_at=AT + timedelta(hours=1), expires_at=AT + timedelta(hours=2))
        with pytest.raises(CapabilityPublicationConflictError):
            withdraw(tx, repo, at=AT + timedelta(minutes=1))
        withdrawn = withdraw(tx, repo, at=AT + timedelta(hours=1), reason="")
        assert withdrawn.end_reason == ""


@pytest.mark.parametrize(
    "field,bad",
    [
        ("owner_id", " "),
        ("owner_id", "a" * 513),
        ("capability_id", ""),
        ("capability_id", "a" * 513),
        ("audience_id", None),
        ("audience_id", "a" * 513),
        ("capability_revision", ""),
        ("capability_revision", "a" * 513),
        ("metadata_ciphertext", ""),
        ("metadata_ciphertext", "a" * 65_537),
        ("metadata_digest", "A" * 64),
        ("metadata_digest", "a" * 63),
        ("expected_revision", True),
        ("expected_revision", -1),
        ("expected_revision", 2**63),
        ("expected_revision", 1.5),
    ],
)
def test_publish_validation_bounds(database, field, bad):
    with database.transaction() as tx:
        with pytest.raises(RepositoryValidationError):
            publish(tx, **{field: bad})
        assert tx.fetch_all("SELECT * FROM capability_publication") == ()


@pytest.mark.parametrize(
    "field,bad",
    [
        ("published_at", datetime(2026, 10, 10)),
        ("published_at", AT.astimezone(timezone(timedelta(hours=1)))),
        ("expires_at", AT - timedelta(seconds=1)),
        ("expires_at", AT),
    ],
)
def test_publish_lifetime_validation(database, field, bad):
    with database.transaction() as tx:
        with pytest.raises(RepositoryValidationError):
            publish(tx, **{field: bad})
        assert tx.fetch_all("SELECT * FROM capability_publication") == ()


def test_withdraw_and_expire_validation(database):
    repo = CapabilityPublicationRepository()
    with database.transaction() as tx:
        publish(tx, repo)
        for bad in (True, -1, 2**63, 1.5, "1", None):
            with pytest.raises(RepositoryValidationError):
                withdraw(tx, repo, expected_revision=bad)
            with pytest.raises(RepositoryValidationError):
                expire(tx, repo, expected_revision=bad)
        with pytest.raises(RepositoryValidationError):
            withdraw(tx, repo, at=datetime(2026, 10, 10))
        with pytest.raises(RepositoryValidationError):
            expire(tx, repo, at=datetime(2026, 10, 10))
        with pytest.raises(RepositoryValidationError):
            withdraw(tx, repo, reason="a" * 281)
        with pytest.raises(RepositoryValidationError):
            repo.get(
                tx,
                owner_id=OWNER,
                capability_id=CAPABILITY,
                audience_id=AUDIENCE,
                for_update=1,
            )
        with pytest.raises(RepositoryValidationError):
            repo.get_active(
                tx,
                owner_id=OWNER,
                capability_id=CAPABILITY,
                audience_id=AUDIENCE,
                at=AT,
                for_update=1,
            )
        assert repo.get(
            tx, owner_id=OWNER, capability_id=CAPABILITY, audience_id=AUDIENCE
        ).state_revision == 1


@pytest.mark.parametrize("counter", ["withdrawn", "expired"])
def test_revision_exhaustion_is_typed_and_keeps_state(database, counter):
    repo = CapabilityPublicationRepository()
    with database.transaction() as tx:
        publish(tx, repo)
        if counter == "withdrawn":
            withdraw(tx, repo)
        tx.execute(
            "UPDATE capability_publication SET state_revision = %s WHERE owner_id = %s",
            (2**63 - 1, OWNER),
        )
        original = repo.get(tx, owner_id=OWNER, capability_id=CAPABILITY, audience_id=AUDIENCE)
        with pytest.raises(CapabilityPublicationConflictError):
            if counter == "withdrawn":
                publish(
                    tx,
                    repo,
                    expected_revision=2**63 - 1,
                    capability_revision="revision-two",
                )
            else:
                expire(tx, repo, expected_revision=2**63 - 1)
        assert (
            repo.get(tx, owner_id=OWNER, capability_id=CAPABILITY, audience_id=AUDIENCE)
            == original
        )


def test_missing_rows_and_corrupt_rows_fail_closed(database):
    repo = CapabilityPublicationRepository()
    with database.transaction() as tx:
        with pytest.raises(RepositoryNotFoundError):
            withdraw(tx, repo)
        with pytest.raises(RepositoryNotFoundError):
            expire(tx, repo)
        publish(tx, repo)
        tx.execute(
            "UPDATE capability_publication SET publication_state = 'withdrawn', "
            "ended_at = %s, end_reason = 'direct' WHERE owner_id = %s",
            (AT + timedelta(minutes=1), OWNER),
        )
        assert repo.get(
            tx, owner_id=OWNER, capability_id=CAPABILITY, audience_id=AUDIENCE
        ).state is CapabilityPublicationState.WITHDRAWN


def test_public_facades_expose_the_publication_repository():
    catalog = astralplane.create_repository_catalog()
    assert isinstance(catalog.capability_publications, CapabilityPublicationRepository)
    assert catalog.as_mapping()["capability_publications"] is (
        catalog.capability_publications
    )
    assert astralplane.create_capability_publication_repository() is not (
        astralplane.create_capability_publication_repository()
    )