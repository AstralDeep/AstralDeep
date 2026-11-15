"""Real-PostgreSQL tests for astralplane.repositories.agents, assignments, and guidance:
agent changes invalidate selected-input claims without clearing issued liabilities,
and binding never adopts state after a racing retirement.
"""

from dataclasses import asdict, replace

import pytest
from test_assignments_postgres import (
    action,
    control,
    create,
    create_operation,
    outcome,
    reserve,
    start,
    uid,
)
from test_assignments_postgres import database as database
from test_assignments_postgres import repo as repo
from test_assignments_postgres import tx as tx
from test_declarative_agents_postgres import apply
from test_declarative_agents_postgres import command as declaration
from test_guidance_storage_postgres import bind, command, during_owner_wait, note

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.agents import AgentRepository
from astralplane.repositories.assignments import canonical
from astralplane.repositories.guidance import SkillsRepository
from astralplane.repositories.guidance_models import GuidanceReference
from astralplane.repositories.selected_input_models import (
    SelectedAgentReference,
    SelectedInputEnvelope,
)
from tests.fixtures.database_errors import check_violation


def selected_agent(tx, **changes):
    args = dict(owner_id="owner", agent_id="agent-" + uid())
    args.update(changes)
    cmd = declaration(**args)
    created = apply(tx, cmd)
    active = apply(
        tx,
        declaration(
            "activate",
            owner_id=cmd.owner_id,
            agent_id=cmd.agent_id,
            expected_revision=0,
            revision_id=cmd.revision_id,
        ),
    )
    return active, SelectedAgentReference(
        cmd.agent_id, cmd.revision_id, created.revision.definition_digest
    )


def envelope(agent=None, refs=()):
    return SelectedInputEnvelope(tuple(refs), agent, "test_key", "a" * 64)


def counters(record):
    return dict(
        owner_id=record.owner_id,
        assignment_id=record.assignment_id,
        expected_instruction_revision=record.instruction_revision,
        expected_control_epoch=record.control_epoch,
        expected_state_version=record.state_version,
    )


def bind_selected(repo, tx, record, value):
    return repo.bind_selected_input(tx, **counters(record), envelope=value)


def snapshot(repo, tx, record):
    return repo.get_selected_input(tx, owner_id=record.owner_id, assignment_id=record.assignment_id)


def current(repo, tx, record, expected):
    return repo.assert_selected_input_current(tx, **counters(record), expected=expected)


def test_absence_is_distinct_from_old_empty_or_nonempty_header(tx, repo):
    record = create(repo, tx)
    assert snapshot(repo, tx, record) is None
    assert current(repo, tx, record, None) == record
    record = bind(repo, tx, record, ())
    old = snapshot(repo, tx, record)
    assert old is not None and old.envelope is None and old.references == ()
    assert current(repo, tx, record, old) == record
    with pytest.raises(RepositoryConflictError):
        current(repo, tx, record, None)
    _, agent = selected_agent(tx)
    with pytest.raises(RepositoryConflictError):
        bind_selected(repo, tx, record, envelope(agent))


def test_exact_agent_skill_note_envelope_replay_and_no_private_values(tx, repo):
    _, agent = selected_agent(tx)
    skill = SkillsRepository().apply_change(tx, command=command(slug="s-" + uid()[:8]))
    n = note(tx)
    value = envelope(
        agent,
        (
            GuidanceReference("skill", skill.head.skill_id, 1),
            GuidanceReference("note", n.note_id, 1),
        ),
    )
    record = bind_selected(repo, tx, create_operation(repo, tx), value)
    expected = snapshot(repo, tx, record)
    assert expected.envelope == value
    assert current(repo, tx, record, expected) == record
    assert bind_selected(repo, tx, record, value) == record
    with pytest.raises(RepositoryConflictError):
        bind_selected(repo, tx, record, replace(value, combined_binding="b" * 64))
    with pytest.raises(RepositoryConflictError):
        bind(repo, tx, record, value.references)
    row = tx.fetch_one(
        "SELECT selected_input FROM assignment_guidance_selection WHERE assignment_id=%s",
        (record.assignment_id,),
    )
    text = canonical(row["selected_input"])
    assert "synthetic-opaque-ciphertext" not in text
    assert "Use attributed public sources" not in text
    assert set(row["selected_input"]) == set(asdict(value))


@pytest.mark.parametrize("command_name", ["revise", "archive", "delete", "activate"])
def test_agent_changes_invalidate_claims_without_clearing_issued_liability(tx, repo, command_name):
    from test_operation_control_postgres import operation_claim
    from test_operation_payload_postgres import admission

    active, agent = selected_agent(tx)
    if command_name == "activate":
        newer = apply(
            tx,
            declaration(
                "revise",
                owner_id="owner",
                agent_id=agent.agent_id,
                expected_revision=1,
                parent_revision_id=agent.revision_id,
            ),
        )
        active = apply(
            tx,
            declaration(
                "activate",
                owner_id="owner",
                agent_id=agent.agent_id,
                expected_revision=2,
                revision_id=newer.revision.revision_id,
            ),
        )
        bound_agent = replace(agent, revision_id=newer.revision.revision_id)
    else:
        bound_agent = agent
    record = bind_selected(repo, tx, create_operation(repo, tx), envelope(bound_agent))
    expected = snapshot(repo, tx, record)
    running = operation_claim(repo, tx)
    _, _, binding = admission(repo, tx, running)
    issued = action(repo, tx, running.fence)
    permit = start(repo, tx, running.fence, reserve(repo, tx, running.fence, issued), binding)
    pending = action(repo, tx, running.fence)
    reserve(repo, tx, running.fence, pending)
    args = dict(
        owner_id="owner", agent_id=agent.agent_id, expected_revision=active.agent.state_revision
    )
    if command_name == "revise":
        args["parent_revision_id"] = agent.revision_id
    if command_name == "activate":
        args["revision_id"] = agent.revision_id
    cmd = declaration(command_name, **args)
    applied = apply(tx, cmd)
    after = repo.get_assignment(tx, owner_id="owner", assignment_id=record.assignment_id)
    assert after.lifecycle == "paused" and after.safe_error_code == "guidance_changed"
    assert after.control_epoch == record.control_epoch + 1
    assert after.usage["outstanding"]["tool_calls"] == 1
    with pytest.raises(RepositoryConflictError):
        current(repo, tx, after, expected)
    settled = outcome(repo, tx, permit, record.assignment_id)
    assert settled.state == "succeeded" and settled.result["result"] == {}
    charged = repo.get_assignment(tx, owner_id="owner", assignment_id=record.assignment_id)
    outcome(repo, tx, permit, record.assignment_id)
    assert repo.get_assignment(tx, owner_id="owner", assignment_id=record.assignment_id) == charged
    assert apply(tx, cmd).replayed
    assert repo.get_assignment(tx, owner_id="owner", assignment_id=record.assignment_id) == charged
    assert applied.agent.state_revision == active.agent.state_revision + 1


def test_terminal_reference_not_in_active_invalidation_scan(tx, repo):
    active, agent = selected_agent(tx)
    record = bind_selected(repo, tx, create_operation(repo, tx), envelope(agent))
    stopped = control(repo, tx, record, "stop").assignment
    assert (
        tx.fetch_one(
            "SELECT active FROM assignment_selected_agent WHERE assignment_id=%s",
            (record.assignment_id,),
        )["active"]
        is False
    )
    apply(
        tx,
        declaration(
            "archive",
            owner_id="owner",
            agent_id=agent.agent_id,
            expected_revision=active.agent.state_revision,
        ),
    )
    assert repo.get_assignment(tx, owner_id="owner", assignment_id=record.assignment_id) == stopped
    assert snapshot(repo, tx, stopped).envelope.agent == agent
    with pytest.raises(RepositoryConflictError):
        current(repo, tx, stopped, snapshot(repo, tx, stopped))


@pytest.mark.parametrize("change", ["pointer", "status", "foreign", "digest", "missing"])
def test_current_agent_proof_refuses_even_without_reverse_invalidation(tx, repo, change):
    _, agent = selected_agent(tx)
    value = envelope(agent)
    record = bind_selected(repo, tx, create_operation(repo, tx), value)
    expected = snapshot(repo, tx, record)
    if change in {"pointer", "status"}:
        tx.execute(
            "UPDATE user_agent SET status='draft',selected_definition_revision_id=NULL "
            "WHERE agent_id=%s",
            (agent.agent_id,),
        )
    elif change == "foreign":
        with pytest.raises(RepositoryNotFoundError):
            repo.get_selected_input(tx, owner_id="other", assignment_id=record.assignment_id)
        return
    elif change == "digest":
        with pytest.raises(RepositoryConflictError):
            current(
                repo,
                tx,
                record,
                replace(expected, envelope=replace(value, combined_binding="c" * 64)),
            )
        return
    else:
        tx.execute(
            "DELETE FROM assignment_selected_agent WHERE assignment_id=%s", (record.assignment_id,)
        )
    with pytest.raises((RepositoryConflictError, RepositoryDataError)):
        current(repo, tx, record, expected)


def test_final_clock_after_completion_uses_current_counters_without_execution_fence(
    tx, repo, monkeypatch
):
    from astralplane.repositories import guidance

    now = guidance._clock(tx)
    n = note(tx, expires_at=now + 10000)
    record = bind_selected(
        repo,
        tx,
        create_operation(repo, tx),
        envelope(refs=(GuidanceReference("note", n.note_id, 1),)),
    )
    expected = snapshot(repo, tx, record)
    stopped = control(repo, tx, record, "stop").assignment
    assert current(repo, tx, stopped, expected) == stopped
    observations = iter((n.expires_at - 1, n.expires_at))
    monkeypatch.setattr(guidance, "_clock", lambda tx: next(observations))
    with pytest.raises(RepositoryConflictError):
        current(repo, tx, stopped, expected)


@pytest.mark.postgresql_lock_catalog
def test_envelope_is_detached_before_real_owner_lock_wait(database, repo):
    with database.transaction() as tx:
        tx.execute("DELETE FROM assignment_operation_receipt")
        tx.execute("DELETE FROM persistent_assignment")
        _, agent = selected_agent(tx)
        record = create_operation(repo, tx)
        value = envelope(agent)
    result = during_owner_wait(
        database,
        "owner",
        lambda tx: bind_selected(repo, tx, record, value),
        lambda: object.__setattr__(value, "combined_binding", "b" * 64),
    )
    with database.transaction() as tx:
        assert snapshot(repo, tx, result).envelope.combined_binding == "a" * 64


def test_envelope_and_agent_identity_are_database_immutable(tx, repo):
    _, agent = selected_agent(tx)
    record = bind_selected(repo, tx, create_operation(repo, tx), envelope(agent))
    for query in (
        "UPDATE assignment_guidance_selection SET selected_input=NULL WHERE assignment_id=%s",
        "UPDATE assignment_selected_agent SET agent_id='different' WHERE assignment_id=%s",
    ):
        with check_violation(), tx.savepoint("immutable"):
            tx.execute(query, (record.assignment_id,))


def test_invalid_expected_argument_has_no_database_work(tx, repo):
    record = create(repo, tx)
    with pytest.raises(RepositoryValidationError):
        current(repo, tx, record, {})


@pytest.mark.parametrize("kind", ["missing", "foreign", "wrong_digest", "draft"])
def test_initial_invalid_agent_refuses_without_any_selection_rows(tx, repo, kind):
    _, agent = selected_agent(tx, owner_id="other" if kind == "foreign" else "owner")
    if kind == "missing":
        agent = replace(agent, revision_id=uid())
    elif kind == "wrong_digest":
        agent = replace(agent, definition_digest="f" * 64)
    elif kind == "draft":
        tx.execute(
            "UPDATE user_agent SET status='draft',selected_definition_revision_id=NULL "
            "WHERE agent_id=%s",
            (agent.agent_id,),
        )
    record = create_operation(repo, tx)
    with pytest.raises(RepositoryConflictError, match="guidance_changed"):
        bind_selected(repo, tx, record, envelope(agent))
    assert snapshot(repo, tx, record) is None


@pytest.mark.postgresql_lock_catalog
def test_declarative_command_detaches_before_owner_wait(database):
    cmd = declaration(owner_id="snapshot-owner", agent_id="target-" + uid())
    original = cmd.agent_id
    result = during_owner_wait(
        database,
        cmd.owner_id,
        lambda tx: apply(tx, cmd),
        lambda: object.__setattr__(cmd, "agent_id", "replacement"),
    )
    assert result.agent.agent_id == original


def test_agent_invalidation_failure_rolls_back_all_assignment_and_agent_changes(
    tx, repo, monkeypatch
):
    active, agent = selected_agent(tx)
    record = bind_selected(repo, tx, create_operation(repo, tx), envelope(agent))
    original = AgentRepository.prepare_declarative_command
    cmd = declaration(
        "archive",
        owner_id="owner",
        agent_id=agent.agent_id,
        expected_revision=active.agent.state_revision,
    )
    preparation = original(AgentRepository(), tx, command=cmd)
    original_execute = tx.execute

    def fail(statement, parameters=()):
        result = original_execute(statement, parameters)
        if statement.startswith("UPDATE assignment_selected_agent SET invalidated_at"):
            raise RuntimeError("injected post-invalidation failure")
        return result

    monkeypatch.setattr(tx, "execute", fail)
    with pytest.raises(RuntimeError, match="post-invalidation"):
        AgentRepository().apply_declarative_command(tx, preparation=preparation)
    assert repo.get_assignment(tx, owner_id="owner", assignment_id=record.assignment_id) == record
    assert current(repo, tx, record, snapshot(repo, tx, record)) == record
    assert (
        AgentRepository().get_agent(tx, owner_id="owner", agent_id=agent.agent_id) == active.agent
    )


@pytest.mark.parametrize("boundary", ["assignment", "action"])
@pytest.mark.postgresql_lock_catalog
def test_declaration_locks_work_before_agent_owner_with_real_held_boundary(
    database, repo, boundary
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from test_assignment_execution_guard_postgres import _wait_for_lock
    from test_assignments_postgres import independent_database
    from test_operation_control_postgres import operation_claim

    with database.transaction() as tx:
        tx.execute("DELETE FROM assignment_operation_receipt")
        tx.execute("DELETE FROM persistent_assignment")
        active, agent = selected_agent(tx)
        record = bind_selected(repo, tx, create_operation(repo, tx), envelope(agent))
        running = operation_claim(repo, tx)
        pending = action(repo, tx, running.fence)
        schema = tx.fetch_one("SELECT current_schema() AS s")["s"]
    ready, state = Event(), {}
    cmd = declaration(
        "archive",
        owner_id="owner",
        agent_id=agent.agent_id,
        expected_revision=active.agent.state_revision,
    )

    def author():
        with independent_database(schema) as db, db.transaction() as tx:
            state["pid"] = tx.fetch_one("SELECT pg_backend_pid() AS p")["p"]
            ready.set()
            return apply(tx, cmd)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.transaction() as tx:
            blocker = tx.fetch_one("SELECT pg_backend_pid() AS p")["p"]
            if boundary == "assignment":
                tx.fetch_one(
                    "SELECT id FROM persistent_assignment WHERE id=%s FOR UPDATE",
                    (record.assignment_id,),
                )
            else:
                tx.fetch_one(
                    "SELECT id FROM persistent_assignment_action WHERE id=%s FOR UPDATE",
                    (pending.action_id,),
                )
            future = pool.submit(author)
            assert ready.wait(5)
            _wait_for_lock(tx, state["pid"], blocker)
            assert tx.fetch_one(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS ok", ("owner",)
            )["ok"]
        assert future.result(5).agent.status == "archived"
    with database.transaction() as tx:
        after = repo.get_assignment(tx, owner_id="owner", assignment_id=record.assignment_id)
        assert after.lifecycle == "paused"


@pytest.mark.postgresql_lock_catalog
def test_bind_racing_agent_retirement_never_adopts_after_owner_wait(database, repo):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from test_assignment_execution_guard_postgres import _wait_for_lock
    from test_assignments_postgres import independent_database

    with database.transaction() as tx:
        tx.execute("DELETE FROM assignment_operation_receipt")
        tx.execute("DELETE FROM persistent_assignment")
        active, agent = selected_agent(tx)
        record = create_operation(repo, tx)
        schema = tx.fetch_one("SELECT current_schema() AS s")["s"]
    ready, state = Event(), {}

    def bind_waiter():
        with independent_database(schema) as db, db.transaction() as tx:
            state["pid"] = tx.fetch_one("SELECT pg_backend_pid() AS p")["p"]
            ready.set()
            return bind_selected(repo, tx, record, envelope(agent))

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.transaction() as tx:
            blocker = tx.fetch_one("SELECT pg_backend_pid() AS p")["p"]
            tx.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", ("owner",))
            future = pool.submit(bind_waiter)
            assert ready.wait(5)
            _wait_for_lock(tx, state["pid"], blocker)
            apply(
                tx,
                declaration(
                    "archive",
                    owner_id="owner",
                    agent_id=agent.agent_id,
                    expected_revision=active.agent.state_revision,
                ),
            )
        with pytest.raises(RepositoryConflictError):
            future.result(5)
    with database.transaction() as tx:
        assert snapshot(repo, tx, record) is None


@pytest.mark.postgresql_lock_catalog
def test_expected_snapshot_is_detached_before_owner_wait(database, repo):
    with database.transaction() as tx:
        tx.execute("DELETE FROM assignment_operation_receipt")
        tx.execute("DELETE FROM persistent_assignment")
        _, agent = selected_agent(tx)
        record = bind_selected(repo, tx, create_operation(repo, tx), envelope(agent))
        expected = snapshot(repo, tx, record)
    result = during_owner_wait(
        database,
        "owner",
        lambda tx: current(repo, tx, record, expected),
        lambda: object.__setattr__(expected.envelope, "combined_binding", "b" * 64),
    )
    assert result == record


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", 2),
        ("combined_binding", None),
        ("binding_key_id", 1),
        ("agent", {}),
        ("references", {}),
        ("references", [{"kind": "note", "resource_id": "invalid", "revision": 1}]),
        ("references", [{"kind": "skill", "resource_id": uid(), "revision": True}]),
        ("references", [{"kind": "skill", "resource_id": uid(), "revision": 2**53}]),
        ("private_text", "not a metadata field"),
    ],
)
def test_database_closed_envelope_validator_refuses_malformed_shapes(tx, field, value):
    _, agent = selected_agent(tx)
    raw = asdict(envelope(agent))
    raw["references"] = []
    raw[field] = value
    assert (
        tx.fetch_one("SELECT valid_assignment_selected_input(%s::jsonb) AS ok", (canonical(raw),))[
            "ok"
        ]
        is False
    )


def test_retired_owner_cannot_read_or_compare_selected_input(tx, repo):
    _, agent = selected_agent(tx)
    record = bind_selected(repo, tx, create_operation(repo, tx), envelope(agent))
    expected = snapshot(repo, tx, record)
    repo.retire_operations_for_owner(tx, owner_id="owner")
    for read in (lambda: snapshot(repo, tx, record), lambda: current(repo, tx, record, expected)):
        with pytest.raises(RepositoryConflictError, match="owner_retired"):
            read()
    assert tx.fetch_all("SELECT * FROM assignment_selected_agent") == ()


@pytest.mark.parametrize(
    "key",
    [
        "expected_instruction_revision",
        "expected_control_epoch",
        "expected_state_version",
    ],
)
@pytest.mark.parametrize("value", [True, 1.0, 0, 2])
def test_exact_current_counters_never_coerce(tx, repo, key, value):
    record = create_operation(repo, tx)
    args = counters(record)
    args[key] = value
    with pytest.raises((RepositoryConflictError, RepositoryValidationError)):
        repo.assert_selected_input_current(tx, **args, expected=None)


def test_instruction_change_does_not_adopt_original_header(tx, repo):
    from test_operation_control_postgres import mutate

    _, agent = selected_agent(tx)
    record = bind_selected(repo, tx, create_operation(repo, tx), envelope(agent))
    expected = snapshot(repo, tx, record)
    mutate(tx, record, lambda data: data.update(instruction_revision=2))
    changed = repo.get_assignment(tx, owner_id="owner", assignment_id=record.assignment_id)
    with pytest.raises(RepositoryConflictError, match="guidance_changed"):
        current(repo, tx, changed, expected)


def test_unknown_assignment_keeps_opaque_accounting_while_agent_reference_is_invalidated(tx, repo):
    from test_operation_control_postgres import mutate

    active, agent = selected_agent(tx)
    record = bind_selected(repo, tx, create_operation(repo, tx), envelope(agent))
    mutate(tx, record, lambda data: data["operation"].update(version=3))
    before = tx.fetch_one(
        "SELECT * FROM persistent_assignment WHERE id=%s", (record.assignment_id,)
    )
    apply(
        tx,
        declaration(
            "archive",
            owner_id="owner",
            agent_id=agent.agent_id,
            expected_revision=active.agent.state_revision,
        ),
    )
    assert (
        tx.fetch_one("SELECT * FROM persistent_assignment WHERE id=%s", (record.assignment_id,))
        == before
    )
    assert (
        tx.fetch_one(
            "SELECT invalidated_at FROM assignment_selected_agent WHERE assignment_id=%s",
            (record.assignment_id,),
        )["invalidated_at"]
        is not None
    )
