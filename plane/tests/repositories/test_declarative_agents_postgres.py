"""Real-PostgreSQL tests for astralplane.repositories.agents and drafts:
declarative-agent lifecycle, owner isolation, identity-lock ordering, and atomic
rollback on host or metadata failure.
"""

import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest
from test_assignment_execution_guard_postgres import _wait_for_lock
from test_assignments_postgres import independent_database, parallel_transactions

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.agents import AgentRepository, DeclarativeAgentCommand
from tests.fixtures.database_errors import INTEGRITY_ERRORS


@pytest.fixture
def declaration_db(migrated_clone):
    return migrated_clone.database


def test_declarative_create_is_an_immutable_private_draft(declaration_db):
    from astralplane.repositories import agents

    repository = AgentRepository()
    command = agents.DeclarativeAgentCommand(
        owner_id="definition-owner",
        agent_id="definition-agent",
        command_id=str(uuid.uuid4()),
        command="create",
        revision_id=str(uuid.uuid4()),
        display_name="Research definition",
        definition={"version": 1, "purpose": "Read"},
    )
    with declaration_db.transaction() as tx:
        prepared = repository.prepare_declarative_command(tx, command=command)
        assert prepared.agent is None and not prepared.replayed
        result = repository.apply_declarative_command(tx, preparation=prepared)
        assert result.agent.agent_kind == "declarative"
        assert result.agent.status == "draft"
        assert result.agent.active_revision_id is None
        assert result.agent.selected_definition_revision_id is None
        assert result.revision.revision_kind == "declarative"
        assert result.revision.state == "definition"
        assert result.revision.artifact_digest is None
        assert result.revision.definition_json["purpose"] == "Read"
        assert repository.get_trust(tx, agent_id=command.agent_id) is None
    with declaration_db.transaction() as tx:
        replay = repository.prepare_declarative_command(tx, command=command)
        assert replay.replayed
        assert replay.receipt == result.receipt


def command(kind="create", **changes):
    values = dict(
        owner_id="definition-owner",
        agent_id="definition-agent",
        command_id=str(uuid.uuid4()),
        command=kind,
    )
    if kind in {"create", "revise"}:
        values.update(
            revision_id=str(uuid.uuid4()),
            display_name="Research",
            definition={"version": 1, "purpose": "Read"},
        )
    values.update(changes)
    return DeclarativeAgentCommand(**values)


def apply(tx, value):
    repo = AgentRepository()
    return repo.apply_declarative_command(
        tx,
        preparation=repo.prepare_declarative_command(tx, command=value),
    )


def rows(tx):
    return tuple(
        tuple(tx.fetch_all("SELECT * FROM " + table + " ORDER BY 1"))
        for table in (
            "user_agent",
            "user_agent_revision",
            "user_agent_command_receipt",
            "agent_trust",
            "agent_ownership",
        )
    )


def test_full_lifecycle_preserves_history_clears_selection_and_replays_current_head(declaration_db):
    initial = command()
    repo = AgentRepository()
    with declaration_db.transaction() as tx:
        created = apply(tx, initial)
        activation = command("activate", expected_revision=0, revision_id=initial.revision_id)
        active = apply(tx, activation)
        assert active.agent.status == "active"
        revised = apply(
            tx,
            command(
                "revise",
                expected_revision=1,
                parent_revision_id=initial.revision_id,
                definition={"version": 1, "purpose": "Changed"},
            ),
        )
        assert revised.agent.status == "draft"
        assert revised.agent.selected_definition_revision_id is None
        assert revised.revision.revision_number == 2
        history = repo.list_revisions(tx, owner_id=initial.owner_id, agent_id=initial.agent_id)
        assert [item.revision_number for item in history] == [2, 1]
        assert repo.list_revisions(
            tx, owner_id=initial.owner_id, agent_id=initial.agent_id, before_revision_number=2
        ) == (created.revision,)
        clone = apply(
            tx,
            command(
                "clone",
                agent_id="copy",
                revision_id=str(uuid.uuid4()),
                display_name="Copy",
                source_agent_id=initial.agent_id,
                source_revision_id=initial.revision_id,
            ),
        )
        assert clone.revision.definition_digest == created.revision.definition_digest
        assert clone.revision.parent_revision_id is None
        assert clone.agent.status == "draft" and clone.agent.owner_email is None
        assert repo.get_ownership(tx, agent_id="copy") is None
        archive = command("archive", expected_revision=2)
        archived = apply(tx, archive)
        deleted = apply(tx, command("delete", expected_revision=3))
        assert deleted.agent.deleted_at is not None and deleted.agent.status == "archived"
        before = rows(tx)
        for original in (initial, activation, archive):
            replay = apply(tx, original)
            assert replay.replayed and replay.agent == deleted.agent
        assert rows(tx) == before
        assert archived.agent.state_revision == 3


@pytest.mark.parametrize("change", ["owner", "body", "target", "revision", "command"])
def test_original_command_cannot_be_rebound(declaration_db, change):
    original = command()
    with declaration_db.transaction() as tx:
        apply(tx, original)
        before = rows(tx)
        modifications = {
            "owner": dict(owner_id="foreign-owner"),
            "body": dict(definition={"version": 1, "purpose": "Other"}),
            "target": dict(agent_id="different-target"),
            "revision": dict(revision_id=str(uuid.uuid4())),
            "command": dict(
                command="clone",
                definition=None,
                source_agent_id="other",
                source_revision_id=str(uuid.uuid4()),
            ),
        }[change]
        with pytest.raises((RepositoryConflictError, RepositoryNotFoundError)):
            apply(tx, replace(original, **modifications))
        assert rows(tx) == before


@pytest.mark.parametrize("kind", ["activate", "revise", "archive", "delete", "clone"])
def test_foreign_owned_revision_or_head_is_never_usable(declaration_db, kind):
    with declaration_db.transaction() as tx:
        first = apply(tx, command())
        if kind == "clone":
            value = command(
                kind,
                owner_id="foreign",
                agent_id="foreign-copy",
                revision_id=str(uuid.uuid4()),
                display_name="Copy",
                source_agent_id=first.agent.agent_id,
                source_revision_id=first.revision.revision_id,
            )
        else:
            values = dict(owner_id="foreign", expected_revision=0)
            if kind == "activate":
                values["revision_id"] = first.revision.revision_id
            elif kind == "revise":
                values["parent_revision_id"] = first.revision.revision_id
            value = command(kind, **values)
        before = rows(tx)
        with pytest.raises(RepositoryNotFoundError):
            apply(tx, value)
        assert rows(tx) == before


def test_stale_preparation_and_caught_receipt_failure_leave_no_partial_writes(declaration_db):
    repo = AgentRepository()
    with declaration_db.transaction() as tx:
        initial = apply(tx, command())
        activation = command(
            "activate", expected_revision=0, revision_id=initial.revision.revision_id
        )
        prepared = repo.prepare_declarative_command(tx, command=activation)
        apply(tx, command("archive", expected_revision=0))
        before = rows(tx)
        with pytest.raises(RepositoryConflictError):
            repo.apply_declarative_command(tx, preparation=prepared)
        assert rows(tx) == before
    with declaration_db.transaction() as tx:
        candidate = command(agent_id="rollback-target")
        prepared = repo.prepare_declarative_command(tx, command=candidate)

        class FailReceipt:
            def __getattr__(self, name):
                return getattr(tx, name)

            def fetch_one(self, statement, parameters=()):
                if statement.startswith("INSERT INTO user_agent_command_receipt"):
                    raise RuntimeError("synthetic receipt boundary failure")
                return tx.fetch_one(statement, parameters)

        before = rows(tx)
        with pytest.raises(RuntimeError, match="receipt boundary"):
            repo.apply_declarative_command(FailReceipt(), preparation=prepared)
        assert rows(tx) == before
    with declaration_db.transaction() as tx:
        assert repo.get_agent(tx, owner_id=candidate.owner_id, agent_id=candidate.agent_id) is None


def test_host_final_guard_failure_rolls_back_audit_and_all_metadata(declaration_db):
    with (
        pytest.raises(RuntimeError, match="host authority expired"),
        declaration_db.transaction() as tx,
    ):
        value = command()
        prepared = AgentRepository().prepare_declarative_command(tx, command=value)
        tx.execute(
            "INSERT INTO schema_meta(key,value) VALUES ('synthetic-authoring-audit','event')"
        )
        AgentRepository().apply_declarative_command(tx, preparation=prepared)
        raise RuntimeError("host authority expired")
    with declaration_db.transaction() as tx:
        assert not tx.fetch_all("SELECT * FROM user_agent")
        assert (
            tx.fetch_one("SELECT * FROM schema_meta WHERE key='synthetic-authoring-audit'") is None
        )


def test_receipt_capacity_reserves_both_retirement_transitions(declaration_db):
    with declaration_db.transaction() as tx:
        value = command()
        created = apply(tx, value)
        active = apply(tx, command("activate", expected_revision=0, revision_id=value.revision_id))
        tx.execute(
            "WITH RECURSIVE counts(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM counts WHERE n<4092) "
            "INSERT INTO user_agent_command_receipt(owner_user_id,agent_id,command_id,"
            "command_version,command,request_digest,result_state_revision) "
            "SELECT %s,%s,gen_random_uuid(),1,'activate',%s,1 FROM counts",
            (value.owner_id, value.agent_id, "a" * 64),
        )
        with pytest.raises(RepositoryConflictError, match="capacity"):
            apply(
                tx,
                command(
                    "revise", expected_revision=1, parent_revision_id=created.revision.revision_id
                ),
            )
        archived = apply(tx, command("archive", expected_revision=active.agent.state_revision))
        with pytest.raises(RepositoryConflictError, match="capacity"):
            apply(
                tx,
                command(
                    "activate",
                    expected_revision=archived.agent.state_revision,
                    revision_id=value.revision_id,
                ),
            )
        deleted_command = command("delete", expected_revision=archived.agent.state_revision)
        deleted = apply(tx, deleted_command)
        assert deleted.agent.deleted_at is not None
        assert (
            tx.fetch_one("SELECT count(*) AS count FROM user_agent_command_receipt")["count"]
            == 4096
        )
        assert apply(tx, deleted_command).replayed


def test_two_identical_commands_commit_one_revision_and_receipt(declaration_db):
    value = command()
    results = parallel_transactions(
        declaration_db, (lambda tx: apply(tx, value), lambda tx: apply(tx, value))
    )
    assert sorted(item.replayed for item in results) == [False, True]
    assert results[0].receipt == results[1].receipt
    with declaration_db.transaction() as tx:
        assert len(tx.fetch_all("SELECT * FROM user_agent_revision")) == 1


@pytest.mark.parametrize("first", ["declarative", "legacy"])
@pytest.mark.parametrize("legacy_kind", ["trust", "ownership"])
@pytest.mark.postgresql_lock_catalog
def test_absent_identity_lock_serializes_legacy_trust_and_declaration(
    declaration_db, first, legacy_kind
):
    repo, value = AgentRepository(), command()
    ready, identities = Event(), {}
    with declaration_db.transaction() as tx:
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]

    def legacy(tx):
        if legacy_kind == "ownership":
            return repo.upsert_ownership(
                tx,
                agent_id=value.agent_id,
                owner_email="legacy@example.test",
                is_public=False,
                observed_at=1,
            )
        return repo.set_trust(tx, agent_id=value.agent_id, is_safe=True, marked_by="legacy")

    def run():
        with independent_database(schema) as db, db.transaction() as tx:
            identities["waiter"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            ready.set()
            try:
                return legacy(tx) if first == "declarative" else apply(tx, value)
            except RepositoryConflictError as exc:
                return exc

    with ThreadPoolExecutor(max_workers=1) as workers:
        with declaration_db.transaction() as tx:
            blocker = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            if first == "declarative":
                apply(tx, value)
            else:
                legacy(tx)
            future = workers.submit(run)
            assert ready.wait(5)
            _wait_for_lock(tx, identities["waiter"], blocker)
        assert isinstance(future.result(5), RepositoryConflictError)
    with declaration_db.transaction() as tx:
        record = repo.get_agent(tx, owner_id=value.owner_id, agent_id=value.agent_id)
        assert (record is not None) == (first == "declarative")
        if record is not None:
            assert repo.get_trust(tx, agent_id=value.agent_id) is None
            assert repo.get_ownership(tx, agent_id=value.agent_id) is None


@pytest.mark.parametrize(
    "method",
    ["create", "cas", "revision", "transition", "trust", "ownership", "visibility", "remove"],
)
def test_every_legacy_identity_mutator_cannot_change_a_definition(declaration_db, method):
    repo = AgentRepository()
    with declaration_db.transaction() as tx:
        value = command()
        result = apply(tx, value)
        before = rows(tx)
        calls = {
            "create": lambda: repo.create_agent(
                tx,
                agent_id=value.agent_id,
                owner_id=value.owner_id,
                display_name="Executable",
                observed_at=1,
            ),
            "cas": lambda: repo.compare_and_set_agent(
                tx,
                owner_id=value.owner_id,
                agent_id=value.agent_id,
                expected_revision=0,
                updates={"status": "live"},
            ),
            "revision": lambda: repo.create_revision(
                tx,
                owner_id=value.owner_id,
                agent_id=value.agent_id,
                revision_id=str(uuid.uuid4()),
                revision_number=2,
                compatibility_state="legacy_pending",
                state="legacy_pending",
            ),
            "transition": lambda: repo.transition_revision(
                tx,
                owner_id=value.owner_id,
                agent_id=value.agent_id,
                revision_id=result.revision.revision_id,
                expected_revision=0,
                expected_state="definition",
                updates={"state": "active"},
            ),
            "trust": lambda: repo.set_trust(
                tx, agent_id=value.agent_id, is_safe=True, marked_by="admin"
            ),
            "ownership": lambda: repo.upsert_ownership(
                tx,
                agent_id=value.agent_id,
                owner_email="legacy@example.test",
                is_public=True,
                observed_at=1,
            ),
            "visibility": lambda: repo.set_visibility(
                tx,
                agent_id=value.agent_id,
                owner_email="legacy@example.test",
                is_public=True,
                updated_at=1,
            ),
            "remove": lambda: repo.remove_ownership(
                tx, agent_id=value.agent_id, owner_email="legacy@example.test"
            ),
        }
        if method == "remove":
            assert calls[method]() is False
        else:

            with (
                pytest.raises(
                    (RepositoryConflictError, RepositoryNotFoundError, *INTEGRITY_ERRORS)
                ),
                tx.savepoint("legacy_refusal"),
            ):
                calls[method]()
        assert rows(tx) == before


def test_policy_reconciliation_does_not_mark_declarations_as_executable(declaration_db):
    with declaration_db.transaction() as tx:
        value = command()
        before = apply(tx, value)
        result = AgentRepository().reconcile_validation_policy_for_administration(
            tx, policy_revision="changed"
        )
        assert result.agents_marked_for_revalidation == 0
        assert (
            AgentRepository().get_agent(tx, owner_id=value.owner_id, agent_id=value.agent_id)
            == before.agent
        )


def test_accepted_receipt_is_metadata_only_even_if_old_definition_is_corrupt(declaration_db):
    value, repo = command(), AgentRepository()
    with declaration_db.transaction() as tx:
        original = apply(tx, value)
        tx.execute(
            "UPDATE user_agent_revision SET definition_digest=%s WHERE revision_id=%s",
            ("f" * 64, value.revision_id),
        )
        before = rows(tx)
        preparation = repo.prepare_declarative_command(tx, command=value)
        assert preparation.replayed and preparation.revision is None
        replay = repo.apply_declarative_command(tx, preparation=preparation)
        assert replay.replayed and replay.revision is None and replay.receipt == original.receipt
        assert rows(tx) == before
        with pytest.raises(RepositoryDataError):
            apply(tx, command("activate", expected_revision=0, revision_id=value.revision_id))
        assert rows(tx) == before


def test_untyped_preparation_and_command_cannot_enter_a_transaction(declaration_db):
    with declaration_db.transaction() as tx:
        repo = AgentRepository()
        before = rows(tx)
        with pytest.raises(RepositoryValidationError):
            repo.prepare_declarative_command(tx, command={"command": "create"})
        with pytest.raises(RepositoryValidationError):
            repo.apply_declarative_command(tx, preparation={})
        assert rows(tx) == before


@pytest.mark.parametrize("table", ["agent_runtime_instance", "draft_artifact_publication"])
def test_actual_runtime_and_publication_kind_fks_refuse_declarative_revision(declaration_db, table):

    from tests.integration.test_declarative_agents_upgrade import seed_existing_agents

    with declaration_db.transaction() as tx:
        seed_existing_agents(tx)
        result = apply(tx, command(owner_id="legacy-agent-owner"))
        if table == "agent_runtime_instance":
            tx.execute("DELETE FROM agent_runtime_request")
            sql = "UPDATE agent_runtime_instance SET agent_id=%s,revision_id=%s"
            constraint = "agent_runtime_executable_revision_fk"
        else:
            sql = "UPDATE draft_artifact_publication SET target_agent_id=%s,target_revision_id=%s"
            constraint = "draft_publication_executable_revision_fk"
        with pytest.raises(INTEGRITY_ERRORS) as caught, tx.savepoint("kind_refusal"):
            tx.execute(sql, (result.agent.agent_id, result.revision.revision_id))
        if tx.dialect.is_sqlite:
            import sqlite3

            assert caught.value.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY
        else:
            assert caught.value.diag.constraint_name == constraint


def test_legacy_draft_publication_pointer_refuses_definition_through_public_api(declaration_db):

    from astralplane.repositories.drafts import DraftAgentRepository

    with declaration_db.transaction() as tx:
        value = command()
        result = apply(tx, value)
        drafts = DraftAgentRepository()
        draft = drafts.create_draft(
            tx,
            draft_id="unpublished",
            owner_id=value.owner_id,
            agent_name="Executable",
            agent_slug="unpublished",
            description="Draft",
            observed_at=1,
            target_agent_id=value.agent_id,
        )
        with pytest.raises(INTEGRITY_ERRORS) as caught, tx.savepoint("draft_kind_refusal"):
            drafts.compare_and_set_draft(
                tx,
                owner_id=value.owner_id,
                draft_id=draft.draft_id,
                expected_revision=0,
                updates={"published_revision_id": result.revision.revision_id},
                updated_at=2,
            )
        if tx.dialect.is_sqlite:
            import sqlite3

            assert caught.value.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY
        else:
            assert caught.value.diag.constraint_name == "draft_agents_executable_revision_fk"
        assert (
            drafts.get_draft(
                tx, owner_id=value.owner_id, draft_id=draft.draft_id
            ).published_revision_id
            is None
        )


@pytest.mark.postgresql_lock_catalog
def test_create_before_publication_cannot_take_identity_before_its_owner_lock(declaration_db):
    repo, value = AgentRepository(), command()
    with declaration_db.transaction() as tx:
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
    ready, identities = Event(), {}

    def run():
        with independent_database(schema) as db, db.transaction() as tx:
            identities["waiter"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            ready.set()
            try:
                repo.create_agent(
                    tx,
                    owner_id=value.owner_id,
                    agent_id=value.agent_id,
                    display_name="Executable",
                    observed_at=1,
                )
                repo.lock_owner(tx, owner_id=value.owner_id)
            except RepositoryConflictError as exc:
                return exc

    with ThreadPoolExecutor(max_workers=1) as workers:
        with declaration_db.transaction() as tx:
            repo.lock_declarative_owner(tx, owner_id=value.owner_id)
            blocker = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            future = workers.submit(run)
            assert ready.wait(5)
            _wait_for_lock(tx, identities["waiter"], blocker)
            from astralplane.repositories.agents import _AGENT_IDENTITY_LOCK_NAMESPACE

            assert tx.fetch_one(
                "SELECT pg_try_advisory_xact_lock(%s,hashtext(%s)) AS acquired",
                (_AGENT_IDENTITY_LOCK_NAMESPACE, value.agent_id),
            )["acquired"]
            apply(tx, value)
        assert isinstance(future.result(5), RepositoryConflictError)


@pytest.mark.postgresql_lock_catalog
def test_existing_executable_head_then_ownership_has_no_identity_lock_inversion(declaration_db):
    repo, value = AgentRepository(), command()
    with declaration_db.transaction() as tx:
        repo.create_agent(
            tx,
            owner_id=value.owner_id,
            agent_id=value.agent_id,
            display_name="Executable",
            observed_at=1,
        )
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
    ready, identities = Event(), {}

    def run():
        with independent_database(schema) as db, db.transaction() as tx:
            identities["waiter"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            ready.set()
            with pytest.raises(RepositoryConflictError):
                apply(tx, value)

    with ThreadPoolExecutor(max_workers=1) as workers:
        with declaration_db.transaction() as tx:
            blocker = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            current = repo.get_agent(
                tx, owner_id=value.owner_id, agent_id=value.agent_id, for_update=True
            )
            repo.compare_and_set_agent(
                tx,
                owner_id=value.owner_id,
                agent_id=value.agent_id,
                expected_revision=current.state_revision,
                updates={"status": "live"},
            )
            future = workers.submit(run)
            assert ready.wait(5)
            _wait_for_lock(tx, identities["waiter"], blocker)
            tx.execute("SET LOCAL lock_timeout='300ms'")
            with tx.savepoint("existing_identity"):
                result = repo.upsert_ownership(
                    tx,
                    agent_id=value.agent_id,
                    owner_email="legacy@example.test",
                    is_public=False,
                    observed_at=2,
                )
                assert result.agent_id == value.agent_id
        future.result(5)


@pytest.mark.parametrize("change", ["owner_retirement", "head_revision"])
@pytest.mark.postgresql_lock_catalog
def test_preparation_rechecks_owner_and_head_after_actual_lock_wait(declaration_db, change):
    value = command()
    with declaration_db.transaction() as tx:
        apply(tx, value)
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
    ready, identities = Event(), {}
    attempt = value if change == "owner_retirement" else command("archive", expected_revision=0)

    def run():
        with independent_database(schema) as db, db.transaction() as tx:
            identities["waiter"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            ready.set()
            with pytest.raises(RepositoryConflictError):
                AgentRepository().prepare_declarative_command(tx, command=attempt)

    with ThreadPoolExecutor(max_workers=1) as workers:
        with declaration_db.transaction() as tx:
            blocker = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            if change == "owner_retirement":
                tx.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", (value.owner_id,)
                )
                tx.execute(
                    "INSERT INTO astralplane_blob_owner_state(owner_id,state,retired_at) "
                    "VALUES (%s,'retired',clock_timestamp())",
                    (value.owner_id,),
                )
            else:
                tx.execute(
                    "UPDATE user_agent SET state_revision=1 WHERE agent_id=%s", (value.agent_id,)
                )
            future = workers.submit(run)
            assert ready.wait(5)
            _wait_for_lock(tx, identities["waiter"], blocker)
        future.result(5)
    with declaration_db.transaction() as tx:
        assert tx.fetch_one("SELECT count(*) AS n FROM user_agent_command_receipt")["n"] == 1


@pytest.mark.parametrize(
    "loss",
    [
        "retired",
        "stale_parent",
        "stale_revision",
        "missing_revision",
        "archived_noop",
        "deleted_noop",
    ],
)
def test_owner_and_exact_state_losses_refuse_without_partial_mutation(declaration_db, loss):
    with declaration_db.transaction() as tx:
        value = command()
        initial = apply(tx, value)
        if loss == "retired":
            tx.execute(
                "INSERT INTO astralplane_blob_owner_state(owner_id,state,retired_at) "
                "VALUES (%s,'retired',clock_timestamp())",
                (value.owner_id,),
            )
            attempt = value
        elif loss == "stale_parent":
            attempt = command("revise", expected_revision=0, parent_revision_id=str(uuid.uuid4()))
        elif loss == "stale_revision":
            attempt = command("archive", expected_revision=1)
        elif loss == "missing_revision":
            attempt = command("activate", expected_revision=0, revision_id=str(uuid.uuid4()))
        elif loss == "archived_noop":
            archived = apply(tx, command("archive", expected_revision=0))
            attempt = command("archive", expected_revision=archived.agent.state_revision)
        else:
            apply(tx, command("delete", expected_revision=initial.agent.state_revision))
            attempt = command("delete", expected_revision=1)
        before = rows(tx)
        with pytest.raises((RepositoryConflictError, RepositoryNotFoundError)):
            apply(tx, attempt)
        assert rows(tx) == before
