"""Real-PostgreSQL tests for astralplane.repositories.history and workspaces: a complete
destination read is read-only and bounded, refuses before payload fetch when over
budget, and never reveals content for a foreign or changed head.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest
from test_assignments_postgres import database as database
from test_assignments_postgres import independent_database, uid
from test_assignments_postgres import repo as repo

from astralplane.repositories import RepositoryConflictError, RepositoryValidationError
from astralplane.repositories.history import ConversationRepository
from astralplane.repositories.workspaces import (
    CanvasComponentRecord,
    CanvasRepository,
    LayoutRecord,
    LayoutRepository,
    PublicationRepository,
)


def setup(database):
    owner, chat = uid(), uid()
    with database.transaction() as tx:
        schema = tx.fetch_one("SELECT current_schema() AS s")["s"]
        ConversationRepository().create(
            tx,
            owner_id=owner,
            conversation_id=chat,
            title="Synthetic destination",
            agent_id=None,
            created_at=1,
        )
        canvas = CanvasRepository().create(
            tx,
            CanvasComponentRecord(
                uid(),
                chat,
                owner,
                "existing",
                {"type": "text", "content": "reviewed"},
                "text",
                None,
                0,
                1,
                1,
            ),
        )
        layout = LayoutRepository().create(
            tx,
            LayoutRecord(
                0, chat, owner, "main", 0, {"type": "column", "children": ["existing"]}, 1, 1
            ),
        )
    return owner, chat, schema, canvas, layout


def read(repo, tx, owner, chat, **changes):
    method = getattr(repo, "read_result_publication_destination", None)
    assert callable(method), "bounded publication destination read is not implemented"
    return method(
        tx,
        **dict(
            owner_id=owner,
            conversation_id=chat,
            expected_render_revision=0,
            expected_publication_id=None,
            **changes,
        ),
    )


def test_complete_destination_is_read_only_and_over_budget_refuses_before_payload_fetch(
    database, repo, monkeypatch
):
    owner, chat, _, component, layout = setup(database)
    with database.transaction() as tx:
        result = read(repo, tx, owner, chat)
        assert len(result.components) == len(result.layouts) == 1
        assert result.components[0].row_id == component.row_id
        assert result.components[0].payload == component.payload
        assert result.layouts[0].tree == layout.tree
        assert (
            ConversationRepository().get(tx, owner_id=owner, conversation_id=chat).render_revision
            == 0
        )

    def forbidden(*args, **kwargs):
        raise AssertionError("over-budget content must never enter Python")

    monkeypatch.setattr(CanvasRepository, "list_current", forbidden)
    monkeypatch.setattr(LayoutRepository, "list_current", forbidden)
    with (
        database.transaction() as tx,
        pytest.raises(RepositoryConflictError, match="publication_conflict"),
    ):
        read(repo, tx, owner, chat, maximum_bytes=10)


@pytest.mark.parametrize(
    "changes",
    [
        {"maximum_bytes": True},
        {"maximum_bytes": 0},
        {"maximum_bytes": 8 * 1024 * 1024 + 1},
        {"expected_render_revision": True},
        {"expected_render_revision": -1},
        {"expected_publication_id": "not-a-uuid"},
    ],
)
def test_closed_destination_arguments_refuse(database, repo, changes):
    owner, chat, _, _, _ = setup(database)
    with database.transaction() as tx, pytest.raises(RepositoryValidationError):
        args = dict(
            owner_id=owner,
            conversation_id=chat,
            expected_render_revision=0,
            expected_publication_id=None,
        )
        args.update(changes)
        repo.read_result_publication_destination(tx, **args)


@pytest.mark.parametrize("field", ["owner", "chat", "revision"])
def test_foreign_missing_or_changed_head_cannot_reveal_content(database, repo, field):
    owner, chat, _, _, _ = setup(database)
    with database.transaction() as tx, pytest.raises(RepositoryConflictError):
        repo.read_result_publication_destination(
            tx,
            owner_id=uid() if field == "owner" else owner,
            conversation_id=uid() if field == "chat" else chat,
            expected_render_revision=1 if field == "revision" else 0,
            expected_publication_id=None,
        )


@pytest.mark.parametrize("kind", ["canvas", "layout", "insert"])
def test_prior_legacy_writer_refuses_read_without_wait_and_savepoint_recovers(database, repo, kind):
    owner, chat, schema, canvas, layout = setup(database)

    def inspect():
        with independent_database(schema) as db, db.transaction() as tx:
            with pytest.raises(RepositoryConflictError, match="publication_busy"):
                read(repo, tx, owner, chat)
            assert tx.fetch_one("SELECT 1 AS n")["n"] == 1

    with ThreadPoolExecutor(max_workers=1) as pool, database.transaction() as tx:
        if kind == "canvas":
            CanvasRepository().replace(
                tx,
                owner_id=owner,
                conversation_id=chat,
                component_id=canvas.component_id,
                payload={"grown": "x" * 2000},
                component_type="text",
                title=None,
                expected_updated_at=1,
                updated_at=2,
            )
        elif kind == "layout":
            LayoutRepository().replace(
                tx,
                owner_id=owner,
                conversation_id=chat,
                layout_key=layout.layout_key,
                tree={"grown": "x" * 2000},
                expected_updated_at=1,
                updated_at=2,
            )
        else:
            CanvasRepository().create(
                tx, replace(canvas, row_id=uid(), component_id="phantom", position=1)
            )
        pool.submit(inspect).result(5)


@pytest.mark.parametrize("kind", ["canvas", "layout", "insert"])
def test_held_complete_read_blocks_legacy_enlargement_and_null_head_phantoms(database, repo, kind):
    owner, chat, schema, canvas, layout = setup(database)
    entered = Event()

    def mutate():
        with independent_database(schema) as db, db.transaction() as tx:
            tx.fetch_one("SELECT set_config('lock_timeout','80ms',true)")
            entered.set()
            with pytest.raises(Exception) as refused:
                if kind == "canvas":
                    CanvasRepository().replace(
                        tx,
                        owner_id=owner,
                        conversation_id=chat,
                        component_id=canvas.component_id,
                        payload={"grown": "x" * 2000},
                        component_type="text",
                        title=None,
                        expected_updated_at=1,
                        updated_at=2,
                    )
                elif kind == "layout":
                    LayoutRepository().replace(
                        tx,
                        owner_id=owner,
                        conversation_id=chat,
                        layout_key=layout.layout_key,
                        tree={"grown": "x" * 2000},
                        expected_updated_at=1,
                        updated_at=2,
                    )
                else:
                    CanvasRepository().create(
                        tx, replace(canvas, row_id=uid(), component_id="phantom", position=1)
                    )
            assert getattr(refused.value, "pgcode", None) == "55P03"
            raise RuntimeError("rollback expected failed writer")

    with ThreadPoolExecutor(max_workers=1) as pool, database.transaction() as tx:
        first = read(repo, tx, owner, chat)
        pending = pool.submit(mutate)
        assert entered.wait(2)
        with pytest.raises(RuntimeError, match="rollback expected"):
            pending.result(5)
        assert read(repo, tx, owner, chat) == first


def test_empty_and_committed_destination_preserve_exact_scope_and_detached_content(database, repo):
    owner, chat, _, canvas, layout = setup(database)
    publication = uid()
    with database.transaction() as tx:
        now = tx.fetch_one("SELECT clock_timestamp() AS t")["t"]
        pubs = PublicationRepository()
        pubs.stage(
            tx,
            owner_id=owner,
            conversation_id=chat,
            publication_id=publication,
            request_generation=uid(),
            base_render_revision=0,
            started_at=now,
        )
        CanvasRepository().create(
            tx,
            replace(canvas, row_id=uid(), publication_id=publication, committed_render_revision=1),
        )
        LayoutRepository().create(
            tx,
            replace(layout, layout_id=0, publication_id=publication, committed_render_revision=1),
        )
        pubs.commit_at_head(
            tx,
            owner_id=owner,
            conversation_id=chat,
            publication_id=publication,
            expected_staged_base_render_revision=0,
            expected_head_render_revision=0,
            expected_head_publication_id=None,
            committed_at=now,
            updated_at=2,
        )
    with database.transaction() as tx:
        result = repo.read_result_publication_destination(
            tx,
            owner_id=owner,
            conversation_id=chat,
            expected_render_revision=1,
            expected_publication_id=publication,
        )
        assert len(result.components) == len(result.layouts) == 1
        assert result.components[0].payload["content"] == "reviewed"
        with pytest.raises(TypeError):
            result.components[0].payload["content"] = "changed"
    empty = uid()
    with database.transaction() as tx:
        ConversationRepository().create(
            tx, owner_id=owner, conversation_id=empty, title="Empty", agent_id=None, created_at=1
        )
        result = read(repo, tx, owner, empty)
        assert result.components == () and result.layouts == ()


def test_count_bound_refuses_before_loading_and_does_not_truncate(database, repo, monkeypatch):
    owner, chat, _, canvas, _ = setup(database)
    with database.transaction() as tx:
        for i in range(1000):
            CanvasRepository().create(
                tx, replace(canvas, row_id=uid(), component_id=f"other-{i}", position=i + 1)
            )

    def forbidden(*args, **kwargs):
        raise AssertionError("too many stored rows must not be loaded")

    monkeypatch.setattr(CanvasRepository, "list_current", forbidden)
    with database.transaction() as tx, pytest.raises(RepositoryConflictError):
        read(repo, tx, owner, chat)


@pytest.mark.parametrize("kind", ["owner_type", "legacy_missing", "retired"])
def test_invalid_owner_or_incomplete_legacy_metadata_never_becomes_reviewable(database, repo, kind):
    owner, chat, _, canvas, _ = setup(database)
    with database.transaction() as tx:
        if kind == "legacy_missing":
            tx.execute(
                "UPDATE saved_components SET component_id=NULL WHERE id=%s", (canvas.row_id,)
            )
        elif kind == "retired":
            tx.execute(
                "INSERT INTO astralplane_blob_owner_state(owner_id,state,retired_at) "
                "VALUES(%s,'retired',clock_timestamp())",
                (owner,),
            )
    with (
        database.transaction() as tx,
        pytest.raises((RepositoryValidationError, RepositoryConflictError)),
    ):
        read(repo, tx, None if kind == "owner_type" else owner, chat)
