"""Real-PostgreSQL tests for astralplane.repositories.history and workspaces: Save
contention refuses both legacy writer lock orders, fresh content stays invisible
until outer commit, and a lost-ack retry still commits exactly once.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event

import pytest
from test_assignment_execution_guard_postgres import _wait_for_lock
from test_assignments_postgres import database as database
from test_assignments_postgres import independent_database, parallel_transactions, uid
from test_assignments_postgres import repo as repo
from test_assignments_postgres import tx as tx
from test_guidance_storage_postgres import during_owner_wait
from test_result_publication_postgres import proposed, state

from astralplane.repositories import RepositoryConflictError
from astralplane.repositories.history import ConversationRepository
from astralplane.repositories.workspaces import PublicationRepository


def setup(database, repo):
    with database.transaction() as tx:
        tx.execute("DELETE FROM assignment_operation_receipt")
        tx.execute("DELETE FROM persistent_assignment")
        tx.execute("DELETE FROM astralplane_blob_owner_state WHERE owner_id='owner'")
        values = proposed(repo, tx)
        schema = tx.fetch_one("SELECT current_schema() AS s")["s"]
    return values, schema


@pytest.mark.parametrize("order", ["chat_first", "publication_first"])
def test_save_refuses_both_legacy_writer_lock_orders_without_wait(database, repo, order):
    (record, proposal, content, args, _), schema = setup(database, repo)
    with database.transaction() as tx:
        old = uid()
        PublicationRepository().stage(
            tx,
            owner_id="owner",
            conversation_id=proposal.conversation_id,
            publication_id=old,
            request_generation=uid(),
            base_render_revision=0,
            started_at=tx.fetch_one("SELECT clock_timestamp() AS now")["now"],
        )

    def save():
        with independent_database(schema) as db, db.transaction() as tx:
            tx.fetch_one("SELECT set_config('lock_timeout','5s',true)")
            with pytest.raises(RepositoryConflictError, match="publication_busy"):
                repo.commit_result_publication(tx, **args, content=content)
            assert tx.fetch_one("SELECT current_setting('lock_timeout') AS v")["v"] == "5s"
            return state(tx, record, proposal)

    with ThreadPoolExecutor(max_workers=1) as pool, database.transaction() as tx:
        locks = (
            ("chats", "id", proposal.conversation_id),
            ("conversation_commit", "commit_id", old),
        )
        for table, key, identity in locks if order == "chat_first" else reversed(locks):
            tx.fetch_one(
                "SELECT " + key + " FROM " + table + " WHERE " + key + "=%s FOR UPDATE", (identity,)
            )
        before = state(tx, record, proposal)
        assert pool.submit(save).result(5) == before


@pytest.mark.postgresql_lock_catalog
def test_absent_publication_unique_fk_cycle_is_bounded_and_restores_timeout(database, repo):
    (record, proposal, content, args, _), schema = setup(database, repo)
    ready, worker = Event(), {}

    def legacy_insert():
        with independent_database(schema) as db, db.transaction() as tx:
            worker["pid"] = tx.fetch_one("SELECT pg_backend_pid() AS p")["p"]
            ready.set()
            return PublicationRepository().stage(
                tx,
                owner_id="owner",
                conversation_id=proposal.conversation_id,
                publication_id=proposal.publication_id,
                request_generation=uid(),
                base_render_revision=0,
                started_at=tx.fetch_one("SELECT clock_timestamp() AS now")["now"],
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.transaction() as tx:
            repo.prepare_result_publication(tx, **args)
            before = state(tx, record, proposal)
            blocker = tx.fetch_one("SELECT pg_backend_pid() AS p")["p"]
            future = pool.submit(legacy_insert)
            assert ready.wait(5)
            _wait_for_lock(tx, worker["pid"], blocker)
            tx.fetch_one("SELECT set_config('lock_timeout','500ms',true)")
            with pytest.raises(RepositoryConflictError, match="publication_busy"):
                repo.commit_result_publication(tx, **args, content=content)
            assert tx.fetch_one("SELECT current_setting('lock_timeout') AS v")["v"] == "500ms"
            assert state(tx, record, proposal) == before
        assert future.result(5).state == "staged"


@pytest.mark.postgresql_schema_catalog
def test_success_preserves_strict_timeout_and_only_immediate_constraints_apply(tx, repo):
    _record, _proposal, content, args, _ = proposed(repo, tx)
    tx.fetch_one("SELECT set_config('lock_timeout','1ms',true)")
    tx.fetch_one("SELECT set_config('statement_timeout','5s',true)")
    relevant = tx.fetch_all(
        "SELECT conname,condeferrable,condeferred FROM pg_constraint "
        "WHERE conrelid IN ('conversation_commit'::regclass,'saved_components'::regclass,"
        "'workspace_layout'::regclass,'chats'::regclass) AND contype IN ('f','p','u')"
    )
    assert relevant and all(not row["condeferrable"] and not row["condeferred"] for row in relevant)
    repo.commit_result_publication(tx, **args, content=content)
    assert tx.fetch_one("SELECT current_setting('lock_timeout') AS v")["v"] == "1ms"
    assert tx.fetch_one("SELECT current_setting('statement_timeout') AS v")["v"] == "5s"


def test_fresh_payload_and_pointer_are_invisible_until_outer_commit(database, repo, monkeypatch):
    (record, proposal, content, args, _), schema = setup(database, repo)
    written, release = Event(), Event()
    original = PublicationRepository.commit_at_head

    def held(self, tx, **kwargs):
        written.set()
        assert release.wait(5)
        return original(self, tx, **kwargs)

    monkeypatch.setattr(PublicationRepository, "commit_at_head", held)

    def save():
        with independent_database(schema) as db, db.transaction() as tx:
            return repo.commit_result_publication(tx, **args, content=content)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(save)
        try:
            assert written.wait(5)
            with database.transaction() as tx:
                assert (
                    tx.fetch_one(
                        "SELECT commit_id FROM conversation_commit WHERE commit_id=%s",
                        (proposal.publication_id,),
                    )
                    is None
                )
                assert (
                    tx.fetch_one(
                        "SELECT id FROM saved_components WHERE conversation_commit_id=%s",
                        (proposal.publication_id,),
                    )
                    is None
                )
                assert (
                    ConversationRepository()
                    .get(tx, owner_id="owner", conversation_id=proposal.conversation_id)
                    .publication_id
                    is None
                )
        finally:
            release.set()
        receipt = future.result(5)
    with database.transaction() as tx:
        after = state(tx, record, proposal)
        replay = repo.prepare_result_publication(
            tx, **dict(args, authority=None, caller_valid_until=None)
        )
        assert replay.replayed and replay.receipt == receipt
        assert state(tx, record, proposal) == after


def test_duplicate_decisions_commit_exactly_one_publication(database, repo):
    (record, proposal, content, args, _), _schema = setup(database, repo)
    receipts = parallel_transactions(
        database,
        tuple(
            lambda tx: repo.commit_result_publication(tx, **args, content=content) for _ in range(2)
        ),
    )
    assert receipts[0] == receipts[1]
    with database.transaction() as tx:
        assert (
            tx.fetch_one(
                "SELECT count(*) AS n FROM conversation_commit WHERE commit_id=%s",
                (proposal.publication_id,),
            )["n"]
            == 1
        )
        assert (
            state(tx, record, proposal)[0]["data"]["state_version"]
            == args["expected_state_version"] + 1
        )


@pytest.mark.postgresql_lock_catalog
def test_copied_content_cannot_change_during_owner_wait(database, repo):
    (_record, proposal, content, args, _), _schema = setup(database, repo)
    before = content.components[0].payload["text"]
    receipt = during_owner_wait(
        database,
        "owner",
        lambda tx: repo.commit_result_publication(tx, **args, content=content),
        lambda: content.components[0].payload.update(text="unreviewed mutation"),
    )
    assert receipt.content_digest == proposal.content_digest
    with database.transaction() as tx:
        payload = tx.fetch_one(
            "SELECT component_data FROM saved_components WHERE id=%s",
            (content.components[0].row_id,),
        )["component_data"]
        assert before in payload and "unreviewed mutation" not in payload


@pytest.mark.parametrize("cutoff", ["caller", "original"])
@pytest.mark.postgresql_lock_catalog
def test_final_db_clock_after_visible_write_refuses_elapsed_original_bounds(
    tx, repo, monkeypatch, cutoff
):
    record, proposal, content, args, _ = proposed(repo, tx)
    now = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
    bound = now + timedelta(milliseconds=200)
    if cutoff == "caller":
        args = dict(args, caller_valid_until=bound)
    else:
        args = dict(args, authority=replace(args["authority"], valid_until=bound))
    before, calls = state(tx, record, proposal), 0
    original = repo.assert_selected_input_current

    def cross_expiry(*values, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            tx.execute("SELECT pg_sleep(0.25)")
            assert tx.fetch_one("SELECT clock_timestamp() AS now")["now"] >= bound
        return original(*values, **kwargs)

    monkeypatch.setattr(repo, "assert_selected_input_current", cross_expiry)
    with pytest.raises(RepositoryConflictError):
        repo.commit_result_publication(tx, **args, content=content)
    assert state(tx, record, proposal) == before


@pytest.mark.postgresql_lock_catalog
def test_original_expiry_is_rechecked_after_held_owner_lock(database, repo):
    (record, proposal, content, args, _), schema = setup(database, repo)
    with database.transaction() as tx:
        now = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
    args = dict(args, caller_valid_until=now + timedelta(milliseconds=200))

    def cross():
        with independent_database(schema) as db, db.transaction() as tx:
            tx.execute("SELECT pg_sleep(0.25)")

    with pytest.raises(RepositoryConflictError):
        during_owner_wait(
            database,
            "owner",
            lambda tx: repo.commit_result_publication(tx, **args, content=content),
            cross,
        )
    with database.transaction() as tx:
        assert state(tx, record, proposal)[2].publication_id is None


@pytest.mark.parametrize("held", ["publication", "component", "layout"])
def test_receipt_read_contention_is_closed_and_keeps_transaction_usable(database, repo, held):
    (record, proposal, content, args, _), schema = setup(database, repo)
    with database.transaction() as tx:
        repo.commit_result_publication(tx, **args, content=content)

    def replay():
        with independent_database(schema) as db, db.transaction() as tx:
            with pytest.raises(RepositoryConflictError, match="publication_busy"):
                repo.prepare_result_publication(
                    tx, **dict(args, authority=None, caller_valid_until=None)
                )
            assert tx.fetch_one("SELECT 1 AS ok")["ok"] == 1

    with ThreadPoolExecutor(max_workers=1) as pool, database.transaction() as tx:
        if held == "publication":
            tx.fetch_one(
                "SELECT commit_id FROM conversation_commit WHERE commit_id=%s FOR UPDATE",
                (proposal.publication_id,),
            )
        else:
            table = "saved_components" if held == "component" else "workspace_layout"
            tx.fetch_all(
                "SELECT id FROM " + table + " WHERE conversation_commit_id=%s FOR UPDATE",
                (proposal.publication_id,),
            )
        before = state(tx, record, proposal)
        pool.submit(replay).result(5)
        assert state(tx, record, proposal) == before
