"""The guarded conversation-branch edge backfills every existing linear chat as one
branch: representative long chats, chats with staged or aborted publications, deleted
chats, and multi-owner data keep identical visible output, parents chain in visible
order, and each chat receives one branch head at its last visible message.
"""

from dataclasses import replace
from itertools import pairwise

import pytest

from astralplane.database import migrations as m
from astralplane.errors import SchemaRevisionError
from astralplane.repositories.history import HistoryRepository
from tests.fixtures.migrated_template import bound_clone

OWNER_A = "branch-upgrade-owner-a"
OWNER_B = "branch-upgrade-owner-b"

VISIBLE_QUERY = """
SELECT message.id, message.chat_id, message.user_id, message.role, message.content,
       message.timestamp, message.conversation_commit_id, message.commit_position,
       message.committed_render_revision
FROM messages AS message
LEFT JOIN conversation_commit AS publication
  ON publication.commit_id = message.conversation_commit_id
 AND publication.chat_id = message.chat_id
 AND publication.owner_user_id = message.user_id
WHERE message.chat_id = %s AND message.user_id = %s
  AND (
    message.conversation_commit_id IS NULL
    OR (
        publication.state = 'committed'
        AND publication.committed_render_revision = message.committed_render_revision
    )
  )
ORDER BY
    COALESCE(message.committed_render_revision, 0) ASC,
    CASE
        WHEN message.conversation_commit_id IS NULL THEN message.timestamp
        ELSE message.commit_position::BIGINT
    END ASC,
    message.id ASC
"""


def prior_runner(database):
    registry = m.MigrationRegistry(
        tuple(
            edge for edge in m.MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.016"
        ),
        current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "089.016"),
        current_schema_verifier_checksum=m.PLANE_SCHEMA_089_016_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            m.PLANE_SCHEMA_089_016_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    )
    assert registry.digest == m.PLANE_SCHEMA_089_016_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="089.016",
        migration_digest=registry.digest,
        read_compatible_from=tuple(
            v for v in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from if v < "089.016"
        ),
        accepted_predecessor_digests=tuple(
            p
            for p in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if p[0] < "089.016"
        ),
    )
    return m.MigrationRunner(database, revision=revision, registry=registry)


@pytest.fixture(scope="module")
def predecessor_template(lineage_templates):
    return lineage_templates.template(prior_runner)


@pytest.fixture
def predecessor(predecessor_template):
    with bound_clone(predecessor_template) as clone:
        yield clone


def runner(database):
    return m.MigrationRunner(
        database,
        revision=m.CURRENT_DATA_PLANE_REVISION,
        registry=m.MIGRATION_REGISTRY,
    )


def _visible_rows(tx, conversation_id, owner_id):
    return tuple(dict(row) for row in tx.fetch_all(VISIBLE_QUERY, (conversation_id, owner_id)))


def _seed(tx):
    chats = {
        "long": ("branch-long-chat", OWNER_A),
        "mixed": ("branch-mixed-chat", OWNER_A),
        "staged": ("branch-staged-chat", OWNER_A),
        "aborted": ("branch-aborted-chat", OWNER_A),
        "empty": ("branch-empty-chat", OWNER_A),
        "foreign": ("branch-foreign-chat", OWNER_B),
        "deleted": ("branch-deleted-chat", OWNER_A),
    }
    for conversation_id, owner_id in chats.values():
        tx.execute(
            "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
            "VALUES (%s, %s, 'Synthetic branch chat', 10, 10)",
            (conversation_id, owner_id),
        )
    for index in range(40):
        tx.execute(
            "INSERT INTO messages (chat_id, user_id, role, content, timestamp) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                chats["long"][0],
                OWNER_A,
                "user" if index % 2 == 0 else "assistant",
                f'{{"text":"long-{index}"}}',
                1000 + index,
            ),
        )
    for index in range(2):
        tx.execute(
            "INSERT INTO messages (chat_id, user_id, role, content, timestamp) "
            "VALUES (%s, %s, 'user', %s, %s)",
            (chats["mixed"][0], OWNER_A, f'{{"text":"legacy-{index}"}}', 2000 + index),
        )
    for index, (base, positions) in enumerate(((0, 3), (1, 2))):
        tx.execute(
            """
            INSERT INTO conversation_commit (
                commit_id, chat_id, owner_user_id, request_generation,
                base_render_revision, committed_render_revision, state,
                started_at, committed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, 'committed', to_timestamp(3000), to_timestamp(3001))
            """,
            (
                f"11111111-1111-4111-8111-11111111111{index}",
                chats["mixed"][0],
                OWNER_A,
                f"22222222-2222-4222-8222-22222222222{index}",
                base,
                base + 1,
            ),
        )
        for position in range(positions):
            tx.execute(
                """
                INSERT INTO messages (
                    chat_id, user_id, role, content, timestamp,
                    conversation_commit_id, commit_position, committed_render_revision
                ) VALUES (%s, %s, 'assistant', %s, %s, %s, %s, %s)
                """,
                (
                    chats["mixed"][0],
                    OWNER_A,
                    f'{{"text":"committed-{index}-{position}"}}',
                    3000 + position,
                    f"11111111-1111-4111-8111-11111111111{index}",
                    position,
                    base + 1,
                ),
            )
    tx.execute("UPDATE chats SET render_revision = 2 WHERE id = %s", (chats["mixed"][0],))
    tx.execute(
        """
        INSERT INTO conversation_commit (
            commit_id, chat_id, owner_user_id, request_generation,
            base_render_revision, state, started_at
        ) VALUES (%s, %s, %s, %s, 0, 'staged', to_timestamp(4000))
        """,
        (
            "33333333-3333-4333-8333-333333333331",
            chats["staged"][0],
            OWNER_A,
            "44444444-4444-4444-8444-444444444441",
        ),
    )
    tx.execute(
        "INSERT INTO messages (chat_id, user_id, role, content, timestamp) "
        "VALUES (%s, %s, 'user', '{\"text\":\"staged-prefix\"}', 2100)",
        (chats["staged"][0], OWNER_A),
    )
    for position in range(2):
        tx.execute(
            """
            INSERT INTO messages (
                chat_id, user_id, role, content, timestamp,
                conversation_commit_id, commit_position, committed_render_revision
            ) VALUES (%s, %s, 'assistant', %s, %s, %s, %s, 1)
            """,
            (
                chats["staged"][0],
                OWNER_A,
                f'{{"text":"staged-{position}"}}',
                4000 + position,
                "33333333-3333-4333-8333-333333333331",
                position,
            ),
        )
    tx.execute(
        """
        INSERT INTO conversation_commit (
            commit_id, chat_id, owner_user_id, request_generation,
            base_render_revision, state, started_at, aborted_at
        ) VALUES (%s, %s, %s, %s, 0, 'aborted', to_timestamp(5000), to_timestamp(5001))
        """,
        (
            "55555555-5555-4555-8555-555555555551",
            chats["aborted"][0],
            OWNER_A,
            "66666666-6666-4666-8666-666666666661",
        ),
    )
    tx.execute(
        """
        INSERT INTO messages (
            chat_id, user_id, role, content, timestamp,
            conversation_commit_id, commit_position, committed_render_revision
        ) VALUES (%s, %s, 'assistant', '{"text":"aborted-0"}', 5000, %s, 0, 1)
        """,
        (chats["aborted"][0], OWNER_A, "55555555-5555-4555-8555-555555555551"),
    )
    for index in range(3):
        tx.execute(
            "INSERT INTO messages (chat_id, user_id, role, content, timestamp) "
            "VALUES (%s, %s, 'user', %s, %s)",
            (chats["foreign"][0], OWNER_B, f'{{"text":"foreign-{index}"}}', 6000 + index),
        )
    for index in range(2):
        tx.execute(
            "INSERT INTO messages (chat_id, user_id, role, content, timestamp) "
            "VALUES (%s, %s, 'user', %s, %s)",
            (chats["deleted"][0], OWNER_A, f'{{"text":"deleted-{index}"}}', 7000 + index),
        )
    tx.execute("DELETE FROM chats WHERE id = %s", (chats["deleted"][0],))
    return {name: identifier for name, (identifier, _) in chats.items()}, {
        name: owner_id for name, (_, owner_id) in chats.items()
    }


def test_populated_upgrade_preserves_visible_output_and_backfills_one_branch(
    predecessor,
):
    database = predecessor.database
    with database.transaction() as tx:
        chats, owners = _seed(tx)
        before = {
            name: _visible_rows(tx, conversation_id, owners[name])
            for name, conversation_id in chats.items()
        }

    report = runner(database).run(expected_revision="089.022")
    assert report.applied_steps == (
        "astralplane-089-conversation-branches",
        "astralplane-089-computer-use",
        "astralplane-089-verification-credentials",
        "astralplane-089-chat-read-receipts",
        "astralplane-089-mesh-event-store",
        "astralplane-089-run-and-learning-ledger",
    )

    with database.transaction() as tx:
        for name, conversation_id in chats.items():
            assert _visible_rows(tx, conversation_id, owners[name]) == before[name]

        history = HistoryRepository()
        for name, conversation_id in chats.items():
            owner_id = owners[name]
            rows = _visible_rows(tx, conversation_id, owner_id)
            records = history.messages.list_visible(
                tx, owner_id=owner_id, conversation_id=conversation_id
            )
            assert [record.message_id for record in records] == [row["id"] for row in rows]

        long_rows = _visible_rows(tx, chats["long"], OWNER_A)
        long_records = history.messages.list_visible(
            tx, owner_id=OWNER_A, conversation_id=chats["long"]
        )
        assert len(long_records) == 40
        assert long_records[0].parent_message_id is None
        for previous, current in pairwise(long_records):
            assert current.parent_message_id == previous.message_id
        assert long_records[-1].message_id == long_rows[-1]["id"]

        mixed_records = history.messages.list_visible(
            tx, owner_id=OWNER_A, conversation_id=chats["mixed"]
        )
        assert len(mixed_records) == 7
        assert mixed_records[0].parent_message_id is None
        for previous, current in pairwise(mixed_records):
            assert current.parent_message_id == previous.message_id
        assert mixed_records[2].publication_id == "11111111-1111-4111-8111-111111111110"

        staged_records = history.messages.list_visible(
            tx, owner_id=OWNER_A, conversation_id=chats["staged"]
        )
        assert [record.parent_message_id for record in staged_records] == [None]
        assert len(staged_records) == 1
        staged_all = tx.fetch_all(
            "SELECT id, parent_message_id FROM messages WHERE chat_id = %s "
            "AND conversation_commit_id = '33333333-3333-4333-8333-333333333331' "
            "ORDER BY commit_position",
            (chats["staged"],),
        )
        assert staged_all[0]["parent_message_id"] == staged_records[0].message_id
        assert staged_all[1]["parent_message_id"] == staged_all[0]["id"]

        aborted_records = history.messages.list_visible(
            tx, owner_id=OWNER_A, conversation_id=chats["aborted"]
        )
        assert aborted_records == ()

        for name in ("empty", "deleted"):
            assert history.messages.list_visible(
                tx, owner_id=owners[name], conversation_id=chats[name]
            ) == ()

        for name, conversation_id in chats.items():
            owner_id = owners[name]
            head = history.conversations.branch_head(
                tx, owner_id=owner_id, conversation_id=conversation_id
            )
            rows = _visible_rows(tx, conversation_id, owner_id)
            expected_head = rows[-1]["id"] if rows else None
            if name == "deleted":
                assert head is None
                continue
            assert head is not None and head.version == 0
            assert head.head_message_id == expected_head

        assert history.messages.list_visible(
            tx, owner_id=OWNER_B, conversation_id=chats["long"]
        ) == ()

    assert runner(database).run(expected_revision="089.022").applied_steps == ()
    with pytest.raises(SchemaRevisionError):
        prior_runner(database).run(expected_revision="089.016")


def test_interrupted_edge_rolls_back_then_normal_runner_recovers_and_backfills(
    predecessor,
):
    database = predecessor.database
    with database.transaction() as tx:
        chats, owners = _seed(tx)
        before = {
            name: _visible_rows(tx, conversation_id, owners[name])
            for name, conversation_id in chats.items()
        }

    def interrupt(tx):
        m.PLANE_SCHEMA_089_017_MIGRATION.operation(tx)
        raise RuntimeError("synthetic interruption")

    registry = m.MigrationRegistry(
        tuple(
            replace(edge, operation=interrupt)
            if edge.target_revision == "089.017"
            else edge
            for edge in m.MIGRATION_REGISTRY.migrations
        ),
        current_schema_verifier=m._verify_current_plane_schema,
        current_schema_verifier_checksum=m.CURRENT_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=m.PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
    )
    interrupted = m.MigrationRunner(
        database, revision=m.CURRENT_DATA_PLANE_REVISION, registry=registry
    )
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        interrupted.run(expected_revision="089.022")
    with database.transaction() as tx:
        assert (
            tx.fetch_one("SELECT to_regclass('chat_branch_head') AS relation")["relation"] is None
        )
        assert tx.fetch_one(
            "SELECT count(*) AS n FROM information_schema.columns "
            "WHERE table_name = 'messages' AND column_name = 'parent_message_id'"
        )["n"] == 0
        assert tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == (
            "089.016"
        )

    report = runner(database).run(expected_revision="089.022")
    assert report.applied_steps == (
        "astralplane-089-conversation-branches",
        "astralplane-089-computer-use",
        "astralplane-089-verification-credentials",
        "astralplane-089-chat-read-receipts",
        "astralplane-089-mesh-event-store",
        "astralplane-089-run-and-learning-ledger",
    )
    with database.transaction() as tx:
        for name, conversation_id in chats.items():
            assert _visible_rows(tx, conversation_id, owners[name]) == before[name]
        history = HistoryRepository()
        records = history.messages.list_visible(
            tx, owner_id=OWNER_A, conversation_id=chats["long"]
        )
        assert len(records) == 40
        for previous, current in pairwise(records):
            assert current.parent_message_id == previous.message_id
        head = history.conversations.branch_head(
            tx, owner_id=OWNER_A, conversation_id=chats["long"]
        )
        assert head is not None and head.head_message_id == records[-1].message_id


@pytest.mark.parametrize(
    "corruption",
    (
        "CREATE TABLE chat_branch_head (wrong TEXT)",
        "ALTER TABLE messages DROP COLUMN commit_position",
    ),
)
def test_namesake_or_damaged_predecessor_is_refused(predecessor, corruption):
    with predecessor.database.transaction() as tx:
        tx.execute(corruption)
    with pytest.raises(SchemaRevisionError):
        runner(predecessor.database).run(expected_revision="089.022")


@pytest.mark.parametrize(
    "corruption",
    (
        "ALTER TABLE chat_branch_head DROP COLUMN version",
        "ALTER TABLE messages DROP COLUMN parent_message_id",
    ),
)
def test_current_catalog_damage_is_refused(predecessor, migrated_clone, corruption):
    with migrated_clone.database.transaction() as tx:
        tx.execute(corruption)
    with pytest.raises(SchemaRevisionError):
        runner(migrated_clone.database).run(expected_revision="089.022")
