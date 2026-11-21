"""Exercises owner-scoped project persistence against real PostgreSQL. Caller rollback,
replay, revision fences, and complete chat pagination are public facade contracts.
"""

import pytest

from astralplane import create_project_repository
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.history import ConversationRepository
from astralplane.repositories.preferences import PreferencesRepository

OWNER = "project-owner"
OTHER = "project-other"
P1 = "10000000-0000-4000-8000-000000000001"
P2 = "10000000-0000-4000-8000-000000000002"


def create(tx, **changes):
    values = dict(owner_id=OWNER, project_id=P1, name="Research", position=0, created_at=10)
    values.update(changes)
    return create_project_repository().create(tx, **values)


def chat(tx, chat_id="chat-a", owner_id=OWNER, created_at=10):
    return ConversationRepository().create(
        tx,
        owner_id=owner_id,
        conversation_id=chat_id,
        title="Synthetic empty chat",
        agent_id=None,
        created_at=created_at,
    )


def test_create_replay_rename_tombstone_and_scope(migrated_clone):
    repo = create_project_repository()
    with migrated_clone.database.transaction() as tx:
        original = create(tx)
        assert create(tx, created_at=20) == original
        assert repo.get(tx, owner_id=OTHER, project_id=P1) is None
        assert not repo.list(tx, owner_id=OTHER).records
        with pytest.raises(RepositoryConflictError):
            create(tx, name="Different")
        with pytest.raises(RepositoryNotFoundError):
            repo.rename(
                tx, owner_id=OTHER, project_id=P1, expected_revision=1, name="Denied", updated_at=20
            )
        renamed = repo.rename(
            tx, owner_id=OWNER, project_id=P1, expected_revision=1, name="Renamed", updated_at=20
        )
        assert renamed.revision == 2
        assert create(tx, created_at=30) == renamed
        with pytest.raises(RepositoryConflictError):
            repo.rename(
                tx, owner_id=OWNER, project_id=P1, expected_revision=1, name="Stale", updated_at=30
            )
        with pytest.raises(RepositoryConflictError):
            repo.delete(tx, owner_id=OWNER, project_id=P1, expected_revision=1)
        assert repo.delete(tx, owner_id=OWNER, project_id=P1, expected_revision=2)
        assert not repo.list(tx, owner_id=OWNER).records
        assert repo.get(tx, owner_id=OWNER, project_id=P1) is None
        with pytest.raises(RepositoryConflictError):
            create(tx, created_at=30)
        with pytest.raises(RepositoryNotFoundError):
            repo.delete(tx, owner_id=OWNER, project_id=P1, expected_revision=3)


def test_attachment_owner_fences_nonempty_and_cascade(migrated_clone):
    repo = create_project_repository()
    with migrated_clone.database.transaction() as tx:
        create(tx)
        create(tx, project_id=P2)
        chat(tx)
        chat(tx, "foreign", OTHER)
        assert repo.attach_chat(tx, owner_id=OWNER, project_id=P1, chat_id="chat-a") == P1
        assert repo.attach_chat(tx, owner_id=OWNER, project_id=P1, chat_id="chat-a") == P1
        assert repo.project_for_chat(tx, owner_id=OWNER, chat_id="chat-a").project_id == P1
        assert repo.project_for_chat(tx, owner_id=OTHER, chat_id="chat-a") is None
        with pytest.raises(RepositoryConflictError):
            repo.attach_chat(tx, owner_id=OWNER, project_id=P2, chat_id="chat-a")
        with pytest.raises(RepositoryNotFoundError):
            repo.attach_chat(tx, owner_id=OWNER, project_id=P1, chat_id="foreign")
        with pytest.raises(RepositoryConflictError):
            repo.delete(tx, owner_id=OWNER, project_id=P1, expected_revision=1)
        tx.execute("DELETE FROM chats WHERE user_id=%s AND id=%s", (OWNER, "chat-a"))
        assert not repo.list_chats(tx, owner_id=OWNER, project_id=P1).records
        assert repo.delete(tx, owner_id=OWNER, project_id=P1, expected_revision=1)
        with pytest.raises(RepositoryNotFoundError):
            repo.attach_chat(tx, owner_id=OWNER, project_id=P1, chat_id="foreign")


def test_atomic_create_chat_attach_rollback(migrated_clone):
    repo = create_project_repository()
    with pytest.raises(RuntimeError), migrated_clone.database.transaction() as tx:
        create(tx)
        chat(tx)
        repo.attach_chat(tx, owner_id=OWNER, project_id=P1, chat_id="chat-a")
        raise RuntimeError("synthetic rollback")
    with migrated_clone.database.transaction() as tx:
        assert repo.get(tx, owner_id=OWNER, project_id=P1) is None
        assert not repo.list_owner_chats_before(tx, owner_id=OWNER, cutoff=100).records


def test_stable_scoped_pagination_includes_empty_legacy_and_archived(migrated_clone):
    repo = create_project_repository()
    with migrated_clone.database.transaction() as tx:
        create(tx)
        create(tx, project_id=P2)
        page = repo.list(tx, owner_id=OWNER, limit=1)
        assert page.records[0].project_id == P1
        assert repo.list(tx, owner_id=OWNER, cursor=page.next_cursor).records[0].project_id == P2
        with pytest.raises(RepositoryValidationError):
            repo.list(tx, owner_id=OTHER, cursor=page.next_cursor)
        for identifier, timestamp in [("a", 1), ("b", 2), ("c", 3), ("new", 100)]:
            chat(tx, identifier, created_at=timestamp)
            repo.attach_chat(tx, owner_id=OWNER, project_id=P1, chat_id=identifier)
        PreferencesRepository().set_chat_flag(
            tx, owner_id=OWNER, conversation_id="c", flag="archived", value=True
        )
        tx.execute("UPDATE chats SET created_at=NULL WHERE id='b'")
        first = repo.list_owner_chats_before(tx, owner_id=OWNER, cutoff=100, limit=1)
        assert first.records[0].chat_id == "a"
        tx.execute("DELETE FROM chats WHERE id='a'")
        second = repo.list_owner_chats_before(
            tx, owner_id=OWNER, cutoff=100, cursor=first.next_cursor, limit=1
        )
        assert second.records[0].chat_id == "b"
        assert second.records[0].created_at is None
        final = repo.list_owner_chats_before(
            tx, owner_id=OWNER, cutoff=100, cursor=second.next_cursor
        )
        assert [row.chat_id for row in final.records] == ["c"]
        assert final.next_cursor is None
        with pytest.raises(RepositoryValidationError):
            repo.list_owner_chats_before(tx, owner_id=OWNER, cutoff=101, cursor=first.next_cursor)
        page = repo.list_chats(tx, owner_id=OWNER, project_id=P1, limit=1)
        assert page.records[0].chat_id == "b"
        assert [
            row.chat_id
            for row in repo.list_chats(
                tx, owner_id=OWNER, project_id=P1, cursor=page.next_cursor
            ).records
        ] == ["c", "new"]
        with pytest.raises(RepositoryValidationError):
            repo.list_chats(tx, owner_id=OWNER, project_id=P2, cursor=page.next_cursor)
        with pytest.raises(RepositoryNotFoundError):
            repo.list_chats(tx, owner_id=OTHER, project_id=P1)


def test_concurrent_create_replays_one_identity(migrated_clone):
    from tests.repositories.test_assignments_postgres import parallel_transactions

    results = parallel_transactions(
        migrated_clone.database,
        (
            lambda tx: create(tx, created_at=10),
            lambda tx: create(tx, created_at=20),
        ),
    )
    assert results[0] == results[1]


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_id": " "},
        {"project_id": "bad"},
        {"name": ""},
        {"name": " bad"},
        {"position": -1},
        {"created_at": True},
        {"name": "bad\x00"},
    ],
)
def test_invalid_create_inputs_are_typed(migrated_clone, changes):
    with migrated_clone.database.transaction() as tx, pytest.raises(RepositoryValidationError):
        create(tx, **changes)


def test_public_project_contract_behaviors():
    from tests.fixtures.migrated_template import bound_clone, current_lineages

    for check in (
        test_create_replay_rename_tombstone_and_scope,
        test_atomic_create_chat_attach_rollback,
        test_concurrent_create_replays_one_identity,
    ):
        with bound_clone(current_lineages().current()) as clone:
            check(clone)


def test_attach_delete_race_has_one_serial_order(migrated_clone):
    from tests.repositories.test_assignments_postgres import parallel_transactions

    repo = create_project_repository()
    with migrated_clone.database.transaction() as tx:
        create(tx)
        chat(tx)

    def attach(tx):
        try:
            return repo.attach_chat(tx, owner_id=OWNER, project_id=P1, chat_id="chat-a")
        except RepositoryNotFoundError as error:
            return error

    attached, deleted = parallel_transactions(
        migrated_clone.database,
        (
            attach,
            lambda tx: repo.delete(tx, owner_id=OWNER, project_id=P1, expected_revision=1),
        ),
    )
    with migrated_clone.database.transaction() as tx:
        if attached == P1:
            assert isinstance(deleted, RepositoryConflictError)
            assert repo.project_for_chat(tx, owner_id=OWNER, chat_id="chat-a") is not None
        else:
            assert isinstance(attached, RepositoryNotFoundError)
            assert deleted is True
            assert repo.project_for_chat(tx, owner_id=OWNER, chat_id="chat-a") is None


def test_same_owner_foreign_keys_refuse_direct_cross_owner_membership(migrated_clone):
    from tests.fixtures.database_errors import INTEGRITY_ERRORS

    with migrated_clone.database.transaction() as tx:
        create(tx)
        chat(tx, owner_id=OTHER)
    with pytest.raises(INTEGRITY_ERRORS), migrated_clone.database.transaction() as tx:
        tx.execute(
            "INSERT INTO chat_project_membership(owner_id,project_id,chat_id) VALUES(%s,%s,%s)",
            (OWNER, P1, "chat-a"),
        )


def test_clock_regression_and_bad_page_inputs(migrated_clone):
    from astralplane import ProjectChatCursor, ProjectCursor

    repo = create_project_repository()
    with migrated_clone.database.transaction() as tx:
        create(tx)
        with pytest.raises(RepositoryConflictError):
            repo.rename(
                tx,
                owner_id=OWNER,
                project_id=P1,
                expected_revision=1,
                name="Research",
                updated_at=9,
            )
        with pytest.raises(RepositoryValidationError):
            repo.get(tx, owner_id=OWNER, project_id=P1, for_update="yes")
        for invalid in (0, 201, True, "10"):
            with pytest.raises(RepositoryValidationError):
                repo.list(tx, owner_id=OWNER, limit=invalid)
        with pytest.raises(RepositoryValidationError):
            repo.list(tx, owner_id=OWNER, cursor=ProjectCursor(OWNER, -1, P1))
        with pytest.raises(RepositoryValidationError):
            repo.list_owner_chats_before(
                tx, owner_id=OWNER, cutoff=100, cursor=ProjectChatCursor(OWNER, None, 100, "")
            )


def test_locked_get_takes_owner_lock_before_row_mutation(migrated_clone):
    from tests.repositories.test_assignments_postgres import independent_database

    repo = create_project_repository()
    with migrated_clone.database.transaction() as tx:
        create(tx)
    with migrated_clone.database.transaction() as tx:
        assert repo.get(tx, owner_id=OWNER, project_id=P1, for_update=True) is not None
        with independent_database(migrated_clone.schema) as peer, peer.transaction() as other:
            assert not other.fetch_one(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,91)) AS locked",
                (OWNER,),
            )["locked"]
        renamed = repo.rename(
            tx,
            owner_id=OWNER,
            project_id=P1,
            expected_revision=1,
            name="Locked rename",
            updated_at=20,
        )
        assert renamed.revision == 2
    with independent_database(migrated_clone.schema) as peer, peer.transaction() as other:
        assert other.fetch_one(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,91)) AS locked",
            (OWNER,),
        )["locked"]


@pytest.mark.parametrize("chat_id", [" chat-a", "chat-a ", "chat\na"])
def test_owner_inventory_reuses_legacy_chat_cursor_without_normalization(migrated_clone, chat_id):
    repo = create_project_repository()
    with migrated_clone.database.transaction() as tx:
        chat(tx, chat_id)
        chat(tx, "zz-final")
        first = repo.list_owner_chats_before(tx, owner_id=OWNER, cutoff=100, limit=1)
        assert first.records[0].chat_id == chat_id
        assert first.next_cursor.chat_id == chat_id
        following = repo.list_owner_chats_before(
            tx,
            owner_id=OWNER,
            cutoff=100,
            cursor=first.next_cursor,
            limit=1,
        )
        assert [row.chat_id for row in following.records] == ["zz-final"]
        assert following.next_cursor is None


@pytest.mark.parametrize("chat_id", [" chat-a", "chat-a ", "chat\na"])
def test_project_membership_preserves_legacy_chat_identity(migrated_clone, chat_id):
    repo = create_project_repository()
    with migrated_clone.database.transaction() as tx:
        create(tx)
        for identifier in (chat_id, "zz-final"):
            chat(tx, identifier)
            assert repo.attach_chat(tx, owner_id=OWNER, project_id=P1, chat_id=identifier) == P1
        assert repo.project_for_chat(tx, owner_id=OWNER, chat_id=chat_id).project_id == P1
        first = repo.list_chats(tx, owner_id=OWNER, project_id=P1, limit=1)
        assert first.records[0].chat_id == chat_id
        assert first.next_cursor.chat_id == chat_id
        following = repo.list_chats(
            tx,
            owner_id=OWNER,
            project_id=P1,
            cursor=first.next_cursor,
            limit=1,
        )
        assert [row.chat_id for row in following.records] == ["zz-final"]
        assert following.next_cursor is None
