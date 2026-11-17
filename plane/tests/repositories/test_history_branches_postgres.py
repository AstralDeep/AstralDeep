"""Real-PostgreSQL regressions for conversation branch trees: parent-chained appends,
the compare-and-set branch head, owner-scoped fork lineage, and the branch path,
sibling, and branch-head reads that Deep and the DirectDownload Mac client consume.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from test_assignments_postgres import database as database

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.history import HistoryRepository
from astralplane.repositories.workspaces import WorkspaceRepository

OWNER = "branch-owner"
FOREIGN = "branch-foreign-owner"

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


@pytest.fixture()
def repo():
    return HistoryRepository()


@pytest.fixture()
def workspace_repo():
    return WorkspaceRepository()


@pytest.fixture()
def tx(database):
    with database.transaction() as transaction:
        yield transaction


def _conversation_id() -> str:
    return "branch-chat-" + uuid4().hex[:12]


@pytest.fixture()
def conversation(tx, repo):
    conversation_id = _conversation_id()
    repo.conversations.create(
        tx,
        conversation_id=conversation_id,
        owner_id=OWNER,
        title="Branch conversation",
        agent_id=None,
        created_at=10,
    )
    return conversation_id


def _append(tx, repo, conversation_id, *, role, content, parent=None, timestamp=100):
    return repo.messages.append(
        tx,
        owner_id=OWNER,
        conversation_id=conversation_id,
        role=role,
        content=content,
        timestamp=timestamp,
        parent_message_id=parent,
    )


def test_append_chains_parents_and_the_branch_path_follows_the_head(
    tx, repo, conversation
):
    first = _append(tx, repo, conversation, role="user", content={"text": "one"})
    second = _append(
        tx, repo, conversation, role="assistant", content={"text": "two"}, parent=first.message_id
    )
    third = _append(
        tx, repo, conversation, role="user", content={"text": "three"}, parent=second.message_id
    )

    assert first.parent_message_id is None
    assert second.parent_message_id == first.message_id
    assert third.parent_message_id == second.message_id

    head = repo.conversations.branch_head(
        tx, owner_id=OWNER, conversation_id=conversation
    )
    assert head is not None
    assert head.head_message_id is None and head.version == 0

    moved = repo.conversations.move_branch_head(
        tx,
        owner_id=OWNER,
        conversation_id=conversation,
        head_message_id=third.message_id,
        expected_version=head.version,
    )
    assert moved.head_message_id == third.message_id and moved.version == 1

    path = repo.messages.list_branch_path(tx, owner_id=OWNER, conversation_id=conversation)
    assert [record.message_id for record in path] == [
        first.message_id,
        second.message_id,
        third.message_id,
    ]
    assert repo.messages.list_branch_heads(
        tx, owner_id=OWNER, conversation_id=conversation
    ) == (third,)
    assert repo.messages.count_message_siblings(
        tx,
        owner_id=OWNER,
        conversation_id=conversation,
        message_id=third.message_id,
    ) == 1


def test_regenerated_reply_forms_sibling_branches_with_counts_and_heads(
    tx, repo, conversation
):
    prompt = _append(tx, repo, conversation, role="user", content={"text": "ask"})
    original = _append(
        tx, repo, conversation, role="assistant", content={"text": "a"}, parent=prompt.message_id
    )
    regenerated = _append(
        tx, repo, conversation, role="assistant", content={"text": "b"}, parent=prompt.message_id
    )

    assert repo.messages.count_message_siblings(
        tx, owner_id=OWNER, conversation_id=conversation, message_id=original.message_id
    ) == 2
    heads = repo.messages.list_branch_heads(tx, owner_id=OWNER, conversation_id=conversation)
    assert {record.message_id for record in heads} == {
        original.message_id,
        regenerated.message_id,
    }

    repo.conversations.move_branch_head(
        tx,
        owner_id=OWNER,
        conversation_id=conversation,
        head_message_id=regenerated.message_id,
        expected_version=0,
    )
    switched = repo.messages.list_branch_path(tx, owner_id=OWNER, conversation_id=conversation)
    assert [record.message_id for record in switched] == [
        prompt.message_id,
        regenerated.message_id,
    ]

    repo.conversations.move_branch_head(
        tx,
        owner_id=OWNER,
        conversation_id=conversation,
        head_message_id=original.message_id,
        expected_version=1,
    )
    reverted = repo.messages.list_branch_path(tx, owner_id=OWNER, conversation_id=conversation)
    assert [record.message_id for record in reverted] == [
        prompt.message_id,
        original.message_id,
    ]


def test_branch_head_move_is_compare_and_set_and_a_stale_write_reloads_and_retries(
    tx, repo, conversation
):
    prompt = _append(tx, repo, conversation, role="user", content={"text": "ask"})
    reply = _append(
        tx, repo, conversation, role="assistant", content={"text": "a"}, parent=prompt.message_id
    )

    with pytest.raises(RepositoryConflictError) as stale:
        repo.conversations.move_branch_head(
            tx,
            owner_id=OWNER,
            conversation_id=conversation,
            head_message_id=reply.message_id,
            expected_version=7,
        )
    assert dict(stale.value.metadata)["expected_version"] == "7"

    current = repo.conversations.branch_head(tx, owner_id=OWNER, conversation_id=conversation)
    assert current is not None and current.version == 0
    moved = repo.conversations.move_branch_head(
        tx,
        owner_id=OWNER,
        conversation_id=conversation,
        head_message_id=reply.message_id,
        expected_version=current.version,
    )
    assert moved.version == 1 and moved.head_message_id == reply.message_id


def test_concurrent_head_moves_admit_exactly_one_winner(migrated_clone, repo):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from tests.integration.test_lifecycle_acceptance import concurrent_database

    conversation_id = _conversation_id()
    with migrated_clone.database.transaction() as setup:
        repo.conversations.create(
            setup,
            conversation_id=conversation_id,
            owner_id=OWNER,
            title="Race conversation",
            agent_id=None,
            created_at=10,
        )
        prompt = _append(setup, repo, conversation_id, role="user", content={"text": "ask"})
        left = _append(
            setup,
            repo,
            conversation_id,
            role="assistant",
            content={"text": "a"},
            parent=prompt.message_id,
        )
        right = _append(
            setup,
            repo,
            conversation_id,
            role="assistant",
            content={"text": "b"},
            parent=prompt.message_id,
        )

    barrier = Barrier(2)

    def move(message_id: int):
        with concurrent_database(migrated_clone) as database:
            try:
                with database.transaction() as race:
                    barrier.wait(timeout=30)
                    return repo.conversations.move_branch_head(
                        race,
                        owner_id=OWNER,
                        conversation_id=conversation_id,
                        head_message_id=message_id,
                        expected_version=0,
                    )
            except RepositoryConflictError:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(move, (left.message_id, right.message_id)))
    assert sorted(result is None for result in outcomes) == [False, True]

    with migrated_clone.database.transaction() as verify:
        head = repo.conversations.branch_head(
            verify, owner_id=OWNER, conversation_id=conversation_id
        )
        assert head is not None and head.version == 1
        assert head.head_message_id in (left.message_id, right.message_id)


def test_branch_reads_and_moves_refuse_foreign_owners_with_typed_errors(
    tx, repo, conversation
):
    prompt = _append(tx, repo, conversation, role="user", content={"text": "ask"})
    repo.conversations.move_branch_head(
        tx,
        owner_id=OWNER,
        conversation_id=conversation,
        head_message_id=prompt.message_id,
        expected_version=0,
    )

    assert repo.conversations.branch_head(
        tx, owner_id=FOREIGN, conversation_id=conversation
    ) is None
    with pytest.raises(RepositoryNotFoundError):
        repo.messages.list_branch_path(tx, owner_id=FOREIGN, conversation_id=conversation)
    with pytest.raises(RepositoryNotFoundError):
        repo.messages.list_branch_heads(tx, owner_id=FOREIGN, conversation_id=conversation)
    with pytest.raises(RepositoryNotFoundError):
        repo.messages.count_message_siblings(
            tx,
            owner_id=FOREIGN,
            conversation_id=conversation,
            message_id=prompt.message_id,
        )
    with pytest.raises(RepositoryNotFoundError):
        repo.conversations.move_branch_head(
            tx,
            owner_id=FOREIGN,
            conversation_id=conversation,
            head_message_id=prompt.message_id,
            expected_version=1,
        )
    with pytest.raises(RepositoryNotFoundError):
        repo.conversations.fork(
            tx,
            owner_id=FOREIGN,
            conversation_id="branch-fork-" + uuid4().hex[:8],
            source_conversation_id=conversation,
            source_message_id=prompt.message_id,
            title="Foreign fork",
            agent_id=None,
            created_at=20,
        )
    with pytest.raises(RepositoryValidationError):
        repo.conversations.move_branch_head(
            tx,
            owner_id=OWNER,
            conversation_id=conversation,
            head_message_id=prompt.message_id,
            expected_version=-1,
        )


def test_append_refuses_foreign_or_invisible_parents(tx, repo, conversation, workspace_repo):
    other = _conversation_id()
    repo.conversations.create(
        tx,
        conversation_id=other,
        owner_id=OWNER,
        title="Other conversation",
        agent_id=None,
        created_at=10,
    )
    foreign_message = _append(tx, repo, other, role="user", content={"text": "elsewhere"})

    with pytest.raises(RepositoryNotFoundError):
        _append(
            tx,
            repo,
            conversation,
            role="user",
            content={"text": "cross"},
            parent=foreign_message.message_id,
        )
    with pytest.raises(RepositoryNotFoundError):
        _append(tx, repo, conversation, role="user", content={"text": "ghost"}, parent=999999)

    publication_id = str(uuid4())
    workspace_repo.publications.stage(
        tx,
        publication_id=publication_id,
        owner_id=OWNER,
        conversation_id=conversation,
        request_generation=str(uuid4()),
        base_render_revision=0,
        started_at=NOW,
    )
    staged = repo.messages.append_next_to_staged_publication(
        tx,
        owner_id=OWNER,
        conversation_id=conversation,
        publication_id=publication_id,
        role="assistant",
        content={"text": "draft"},
        timestamp=500,
    )
    assert staged.parent_message_id is None
    with pytest.raises(RepositoryNotFoundError):
        _append(
            tx,
            repo,
            conversation,
            role="user",
            content={"text": "after staged"},
            parent=staged.message_id,
        )
    assert repo.messages.get(
        tx, owner_id=OWNER, conversation_id=conversation, message_id=staged.message_id
    ) is None


def test_staged_publication_appends_chain_from_the_branch_head(
    tx, repo, conversation, workspace_repo
):
    prompt = _append(tx, repo, conversation, role="user", content={"text": "ask"})
    repo.conversations.move_branch_head(
        tx,
        owner_id=OWNER,
        conversation_id=conversation,
        head_message_id=prompt.message_id,
        expected_version=0,
    )
    publication_id = str(uuid4())
    workspace_repo.publications.stage(
        tx,
        publication_id=publication_id,
        owner_id=OWNER,
        conversation_id=conversation,
        request_generation=str(uuid4()),
        base_render_revision=0,
        started_at=NOW,
    )
    first = repo.messages.append_next_to_staged_publication(
        tx,
        owner_id=OWNER,
        conversation_id=conversation,
        publication_id=publication_id,
        role="assistant",
        content={"text": "part-one"},
        timestamp=500,
    )
    second = repo.messages.append_next_to_staged_publication(
        tx,
        owner_id=OWNER,
        conversation_id=conversation,
        publication_id=publication_id,
        role="assistant",
        content={"text": "part-two"},
        timestamp=501,
    )
    assert first.parent_message_id == prompt.message_id
    assert second.parent_message_id == first.message_id
    assert repo.messages.list_branch_path(tx, owner_id=OWNER, conversation_id=conversation) == (
        prompt,
    )


def test_fork_records_lineage_is_replay_safe_and_refuses_foreign_sources(
    tx, repo, conversation
):
    prompt = _append(tx, repo, conversation, role="user", content={"text": "ask"})
    fork_id = "branch-fork-" + uuid4().hex[:8]
    forked = repo.conversations.fork(
        tx,
        owner_id=OWNER,
        conversation_id=fork_id,
        source_conversation_id=conversation,
        source_message_id=prompt.message_id,
        title="Forked conversation",
        agent_id=None,
        created_at=30,
    )
    assert forked.fork_source_conversation_id == conversation
    assert forked.fork_source_message_id == prompt.message_id
    assert forked.owner_id == OWNER

    replay = repo.conversations.fork(
        tx,
        owner_id=OWNER,
        conversation_id=fork_id,
        source_conversation_id=conversation,
        source_message_id=prompt.message_id,
        title="Forked conversation",
        agent_id=None,
        created_at=30,
    )
    assert replay == forked
    head = repo.conversations.branch_head(tx, owner_id=OWNER, conversation_id=fork_id)
    assert head is not None and head.head_message_id is None and head.version == 0

    with pytest.raises(RepositoryConflictError):
        repo.conversations.fork(
            tx,
            owner_id=OWNER,
            conversation_id=fork_id,
            source_conversation_id=conversation,
            source_message_id=prompt.message_id,
            title="Changed fork",
            agent_id=None,
            created_at=30,
        )
    with pytest.raises(RepositoryNotFoundError):
        repo.conversations.fork(
            tx,
            owner_id=OWNER,
            conversation_id="branch-fork-" + uuid4().hex[:8],
            source_conversation_id=conversation,
            source_message_id=424242,
            title="Missing source",
            agent_id=None,
            created_at=30,
        )
    with pytest.raises(RepositoryValidationError):
        repo.conversations.fork(
            tx,
            owner_id=OWNER,
            conversation_id=conversation,
            source_conversation_id=conversation,
            source_message_id=prompt.message_id,
            title="Self fork",
            agent_id=None,
            created_at=30,
        )

    reply = _append(
        tx,
        repo,
        fork_id,
        role="assistant",
        content={"text": "fork growth"},
    )
    assert reply.parent_message_id is None
    assert repo.messages.list_visible(
        tx, owner_id=OWNER, conversation_id=conversation
    ) == (prompt,)
    assert repo.messages.list_visible(tx, owner_id=OWNER, conversation_id=fork_id) == (reply,)


def test_fork_replays_and_foreign_namespace_conflicts_are_distinct(
    tx, repo, conversation
):
    prompt = _append(tx, repo, conversation, role="user", content={"text": "ask"})
    fork_id = "branch-fork-" + uuid4().hex[:8]
    forked = repo.conversations.fork(
        tx,
        owner_id=OWNER,
        conversation_id=fork_id,
        source_conversation_id=conversation,
        source_message_id=prompt.message_id,
        title="Forked conversation",
        agent_id="agent-1",
        created_at=30,
    )
    assert forked.agent_id == "agent-1"
    assert forked.fork_source_conversation_id == conversation
    assert forked.fork_source_message_id == prompt.message_id

    replay = repo.conversations.fork(
        tx,
        owner_id=OWNER,
        conversation_id=fork_id,
        source_conversation_id=conversation,
        source_message_id=prompt.message_id,
        title="Forked conversation",
        agent_id="agent-1",
        created_at=30,
    )
    assert replay == forked

    foreign_chat = "branch-foreign-namespace-" + uuid4().hex[:8]
    repo.conversations.create(
        tx,
        conversation_id=foreign_chat,
        owner_id=FOREIGN,
        title="Foreign namespace",
        agent_id=None,
        created_at=10,
    )
    with pytest.raises(RepositoryConflictError):
        repo.conversations.fork(
            tx,
            owner_id=OWNER,
            conversation_id=foreign_chat,
            source_conversation_id=conversation,
            source_message_id=prompt.message_id,
            title="Namespace theft",
            agent_id=None,
            created_at=30,
        )


def test_branch_head_moves_report_missing_conversations_as_not_found(
    tx, repo, conversation
):
    prompt = _append(tx, repo, conversation, role="user", content={"text": "ask"})
    with pytest.raises(RepositoryNotFoundError):
        repo.conversations.move_branch_head(
            tx,
            owner_id=FOREIGN,
            conversation_id=conversation,
            head_message_id=None,
            expected_version=0,
        )
    with pytest.raises(RepositoryNotFoundError):
        repo.conversations.move_branch_head(
            tx,
            owner_id=OWNER,
            conversation_id="branch-missing-" + uuid4().hex[:8],
            head_message_id=None,
            expected_version=0,
        )
    moved = repo.conversations.move_branch_head(
        tx,
        owner_id=OWNER,
        conversation_id=conversation,
        head_message_id=prompt.message_id,
        expected_version=0,
    )
    assert moved.head_message_id == prompt.message_id


def test_sibling_counts_refuse_missing_messages(tx, repo, conversation):
    _append(tx, repo, conversation, role="user", content={"text": "ask"})
    with pytest.raises(RepositoryNotFoundError):
        repo.messages.count_message_siblings(
            tx,
            owner_id=OWNER,
            conversation_id=conversation,
            message_id=987654,
        )


def test_staged_append_positions_are_dense_for_the_caller(
    tx, repo, conversation, workspace_repo
):
    publication_id = str(uuid4())
    workspace_repo.publications.stage(
        tx,
        publication_id=publication_id,
        owner_id=OWNER,
        conversation_id=conversation,
        request_generation=str(uuid4()),
        base_render_revision=0,
        started_at=NOW,
    )
    first = repo.messages.append_next_to_staged_publication(
        tx,
        owner_id=OWNER,
        conversation_id=conversation,
        publication_id=publication_id,
        role="assistant",
        content={"text": "part-one"},
        timestamp=500,
    )
    second = repo.messages.append_next_to_staged_publication(
        tx,
        owner_id=OWNER,
        conversation_id=conversation,
        publication_id=publication_id,
        role="assistant",
        content={"text": "part-two"},
        timestamp=501,
    )
    assert (first.commit_position, second.commit_position) == (0, 1)
    assert second.parent_message_id == first.message_id


def test_branch_head_moves_carry_an_optional_snapshot_pointer(tx, repo, conversation):
    prompt = _append(tx, repo, conversation, role="user", content={"text": "ask"})
    tx.execute(
        """
        INSERT INTO workspace_snapshot (
            chat_id, user_id, turn_message_id, cause, components, created_at
        ) VALUES (%s, %s, %s, 'branch-test', '[]', 10)
        """,
        (conversation, OWNER, prompt.message_id),
    )
    snapshot_id = int(tx.fetch_one("SELECT MAX(id) AS id FROM workspace_snapshot")["id"])

    moved = repo.conversations.move_branch_head(
        tx,
        owner_id=OWNER,
        conversation_id=conversation,
        head_message_id=prompt.message_id,
        expected_version=0,
        branch_snapshot_id=snapshot_id,
    )
    assert moved.branch_snapshot_id == snapshot_id
    reloaded = repo.conversations.branch_head(tx, owner_id=OWNER, conversation_id=conversation)
    assert reloaded is not None and reloaded.branch_snapshot_id == snapshot_id

    tx.execute("DELETE FROM workspace_snapshot WHERE id = %s", (snapshot_id,))
    cleared = repo.conversations.branch_head(tx, owner_id=OWNER, conversation_id=conversation)
    assert cleared is not None and cleared.branch_snapshot_id is None


def test_branch_survives_deletion_and_stays_owner_scoped(tx, repo, conversation):
    prompt = _append(tx, repo, conversation, role="user", content={"text": "ask"})
    repo.conversations.move_branch_head(
        tx,
        owner_id=OWNER,
        conversation_id=conversation,
        head_message_id=prompt.message_id,
        expected_version=0,
    )
    fork_id = "branch-fork-" + uuid4().hex[:8]
    repo.conversations.fork(
        tx,
        owner_id=OWNER,
        conversation_id=fork_id,
        source_conversation_id=conversation,
        source_message_id=prompt.message_id,
        title="Fork before delete",
        agent_id=None,
        created_at=40,
    )

    assert repo.conversations.delete(tx, owner_id=OWNER, conversation_id=conversation) is True
    assert repo.conversations.branch_head(
        tx, owner_id=OWNER, conversation_id=conversation
    ) is None
    with pytest.raises(RepositoryNotFoundError):
        repo.messages.list_branch_path(tx, owner_id=OWNER, conversation_id=conversation)
    forked = repo.conversations.get(tx, owner_id=OWNER, conversation_id=fork_id)
    assert forked is not None
    assert forked.fork_source_conversation_id is None
    assert forked.fork_source_message_id is None


def _snapshot(tx, *, conversation_id, owner_id):
    tx.execute(
        """
        INSERT INTO workspace_snapshot (
            chat_id, user_id, turn_message_id, cause, components, created_at
        ) VALUES (%s, %s, NULL, 'branch-test', '[]', 10)
        """,
        (conversation_id, owner_id),
    )
    return int(
        tx.fetch_one(
            "SELECT MAX(id) AS id FROM workspace_snapshot WHERE chat_id = %s AND user_id = %s",
            (conversation_id, owner_id),
        )["id"]
    )


def _assert_head_unchanged(tx, repo, conversation):
    head = repo.conversations.branch_head(tx, owner_id=OWNER, conversation_id=conversation)
    assert head is not None
    assert (head.head_message_id, head.branch_snapshot_id, head.version) == (None, None, 0)


def _refused_snapshot(tx, repo, conversation, snapshot_id):
    with pytest.raises(RepositoryNotFoundError) as refused:
        repo.conversations.move_branch_head(
            tx,
            owner_id=OWNER,
            conversation_id=conversation,
            head_message_id=None,
            expected_version=0,
            branch_snapshot_id=snapshot_id,
        )
    assert dict(refused.value.metadata)["operation"] == "conversation.move_branch_head"
    _assert_head_unchanged(tx, repo, conversation)


def test_branch_head_refuses_another_owners_snapshot(tx, repo, conversation):
    foreign_chat = _conversation_id()
    repo.conversations.create(
        tx,
        conversation_id=foreign_chat,
        owner_id=FOREIGN,
        title="Foreign conversation",
        agent_id=None,
        created_at=10,
    )
    _refused_snapshot(
        tx, repo, conversation, _snapshot(tx, conversation_id=foreign_chat, owner_id=FOREIGN)
    )
    _refused_snapshot(
        tx, repo, conversation, _snapshot(tx, conversation_id=conversation, owner_id=FOREIGN)
    )


def test_branch_head_refuses_another_conversations_snapshot(tx, repo, conversation):
    other_chat = _conversation_id()
    repo.conversations.create(
        tx,
        conversation_id=other_chat,
        owner_id=OWNER,
        title="Sibling conversation",
        agent_id=None,
        created_at=10,
    )
    _refused_snapshot(
        tx, repo, conversation, _snapshot(tx, conversation_id=other_chat, owner_id=OWNER)
    )


def test_branch_head_refuses_a_missing_snapshot(tx, repo, conversation):
    missing = _snapshot(tx, conversation_id=conversation, owner_id=OWNER)
    tx.execute("DELETE FROM workspace_snapshot WHERE id = %s", (missing,))
    _refused_snapshot(tx, repo, conversation, missing)
