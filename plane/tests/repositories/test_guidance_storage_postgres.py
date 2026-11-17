"""Real-PostgreSQL tests for astralplane.repositories.guidance: skill-receipt replay and
CAS, encrypted-note current/expiry lifecycle, and guidance-selection binding,
including behavior under a real held owner lock.
"""

import hashlib
from dataclasses import replace

import pytest
from test_assignments_postgres import (
    action,
    claim,
    control,
    create,
    outcome,
    reserve,
    start,
    uid,
)
from test_assignments_postgres import database as database
from test_assignments_postgres import repo as repo

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.guidance import ExplicitNotesRepository, SkillsRepository
from astralplane.repositories.guidance_models import (
    ExplicitNoteRecord,
    ExplicitNoteTombstone,
    GuidanceReference,
    LegacySkillEntry,
    SkillCommand,
    SkillDefinition,
    legacy_directory_digest,
)
from tests.fixtures.database_errors import check_violation


@pytest.fixture
def tx(database):
    with database.transaction() as transaction:
        transaction.execute("DELETE FROM assignment_operation_receipt")
        transaction.execute("DELETE FROM persistent_assignment")
        transaction.execute("DELETE FROM astralplane_blob_owner_state WHERE owner_id='owner'")
        transaction.execute("DELETE FROM owner_skill_catalog")
        transaction.execute("DELETE FROM owner_skill_head")
        transaction.execute("DELETE FROM explicit_note_current")
        yield transaction


def command(kind="create", **changes):
    values = dict(
        owner_id="owner",
        skill_id=uid(),
        command_id=uid(),
        command=kind,
        expected_revision=0,
        slug="research",
        definition=SkillDefinition("Research", "Use attributed public sources."),
    )
    if kind != "create":
        values.update(slug=None, expected_revision=1)
    if kind == "delete":
        values.update(definition=None)
    values.update(changes)
    return SkillCommand(**values)


def edit(initial, **changes):
    return command("replace", skill_id=initial.skill_id, **changes)


def note(tx, *, repository=None, owner_id="owner", note_id=None, expected_revision=0, **changes):
    repository = repository or ExplicitNotesRepository()
    p = repository.prepare_explicit_note(
        tx, owner_id=owner_id, note_id=note_id or uid(), expected_revision=expected_revision
    )
    values = dict(
        owner_id=owner_id,
        note_id=p.note_id,
        revision=expected_revision + 1,
        category="context",
        enabled=True,
        created_at=p.observed_at_ms if p.current is None else p.current.created_at,
        updated_at=p.observed_at_ms,
        ciphertext=b"synthetic-opaque-ciphertext",
    )
    values.update(changes)
    return repository.put_explicit_note(tx, preparation=p, record=ExplicitNoteRecord(**values))


def bind(repo, tx, assignment, refs):
    return repo.bind_guidance_references(
        tx,
        owner_id=assignment.owner_id,
        assignment_id=assignment.assignment_id,
        expected_instruction_revision=assignment.instruction_revision,
        expected_control_epoch=assignment.control_epoch,
        expected_state_version=assignment.state_version,
        references=tuple(refs),
    )


def test_skill_receipt_replays_original_metadata_after_later_edit_and_delete(tx):
    skills = SkillsRepository()
    initial = command()
    first = skills.apply_change(tx, command=initial)
    changed = skills.apply_change(
        tx,
        command=edit(
            initial, definition=SkillDefinition("New name", "Changed private instructions.")
        ),
    )
    assert changed.head.revision == 2
    replay = skills.apply_change(tx, command=initial)
    assert replay.replayed and replay.revision is None and replay.head == changed.head
    assert replay.receipt == first.receipt
    deleted = skills.apply_change(
        tx, command=command("delete", skill_id=initial.skill_id, expected_revision=2)
    )
    assert deleted.head.revision == 3 and not deleted.head.enabled
    replay = skills.apply_change(tx, command=initial)
    assert replay.replayed and replay.revision is None and replay.head == deleted.head
    assert skills.get(tx, owner_id="owner", skill_id=initial.skill_id) is None
    assert [
        r.revision for r in skills.history(tx, owner_id="owner", skill_id=initial.skill_id)
    ] == [3, 2, 1]
    assert (
        skills.get_revision(tx, owner_id="owner", skill_id=initial.skill_id, revision=1).definition
        == initial.definition
    )
    with pytest.raises(RepositoryConflictError):
        skills.apply_change(tx, command=replace(initial, definition=changed.revision.definition))
    with pytest.raises(RepositoryNotFoundError):
        skills.apply_change(tx, command=edit(initial, expected_revision=3))


def test_skill_stale_cas_cannot_reapply_same_old_body(tx):
    skills = SkillsRepository()
    initial = command()
    skills.apply_change(tx, command=initial)
    skills.apply_change(tx, command=edit(initial))
    with pytest.raises(RepositoryConflictError):
        skills.apply_change(tx, command=edit(initial))
    assert skills.get(tx, owner_id="owner", skill_id=initial.skill_id).revision == 2


def test_alias_is_reserved_while_disabled_but_delete_allows_new_uuid(tx):
    skills = SkillsRepository()
    initial = command(
        definition=SkillDefinition(
            "Research", "Exact original instructions.", alias="read", enabled=False
        )
    )
    skills.apply_change(tx, command=initial)
    with pytest.raises(RepositoryConflictError):
        skills.apply_change(
            tx, command=command(slug="other", definition=replace(initial.definition, name="Other"))
        )
    skills.apply_change(tx, command=command("delete", skill_id=initial.skill_id))
    successor = skills.apply_change(tx, command=replace(initial, skill_id=uid(), command_id=uid()))
    assert successor.head.skill_id != initial.skill_id
    assert skills.get_by_slug(tx, owner_id="owner", slug="research") == successor.head


def test_skill_max20_includes_disabled_and_owner_isolation(tx):
    skills = SkillsRepository()
    for i in range(20):
        skills.apply_change(
            tx,
            command=command(
                slug=f"skill-{i}",
                definition=SkillDefinition("Some skill", "Private instructions.", enabled=False),
            ),
        )
    with pytest.raises(RepositoryConflictError):
        skills.apply_change(tx, command=command())
    other = skills.apply_change(tx, command=command(owner_id="another-owner"))
    assert skills.get(tx, owner_id="owner", skill_id=other.head.skill_id) is None
    assert len(skills.list(tx, owner_id="owner")) == 20
    assert skills.list(tx, owner_id="owner", include_disabled=False) == ()
    with pytest.raises(RepositoryConflictError):
        skills.apply_change(tx, command=command(skill_id=other.head.skill_id))
    assert (
        skills.get_revision(tx, owner_id="owner", skill_id=other.head.skill_id, revision=1) is None
    )


def test_materialization_keeps_exact_raw_bytes_and_original_mapping_after_edit(tx):
    skills = SkillsRepository()
    entries = tuple(
        LegacySkillEntry(
            uid(),
            s,
            SkillDefinition("Legacy skill", "Exact legacy instruction text."),
            f"---\r\nslug: {s}\r\n---\r\nOriginal\r\n".encode(),
            legacy_updated_at=123,
        )
        for s in ("a", "a-", "a-b")
    )
    manifest = legacy_directory_digest("owner", entries)
    result = skills.materialize_legacy_skills(
        tx, owner_id="owner", entries=entries, manifest_digest=manifest
    )
    assert not result.replayed
    for entry in entries:
        r = skills.get_revision(tx, owner_id="owner", skill_id=entry.skill_id, revision=1)
        assert (
            r.legacy_markdown == entry.markdown
            and r.legacy_digest == hashlib.sha256(entry.markdown).hexdigest()
        )
    skills.apply_change(tx, command=edit(entries[0]))
    replay = skills.materialize_legacy_skills(
        tx,
        owner_id="owner",
        entries=tuple(replace(e, skill_id=uid()) for e in entries),
        manifest_digest=manifest,
    )
    assert replay.replayed and replay.mappings == result.mappings
    assert replay.mappings[0].revision == 1
    with pytest.raises(RepositoryConflictError):
        skills.materialize_legacy_skills(
            tx,
            owner_id="owner",
            entries=(
                replace(
                    entries[0],
                    definition=SkillDefinition("Changed", "Different parser interpretation."),
                ),
                *entries[1:],
            ),
            manifest_digest=manifest,
        )


def test_empty_materialization_is_a_durable_cutover_not_absence(tx):
    skills = SkillsRepository()
    manifest = legacy_directory_digest("owner", ())
    first = skills.materialize_legacy_skills(
        tx, owner_id="owner", entries=(), manifest_digest=manifest
    )
    assert first.mappings == () and not first.replayed
    skills.apply_change(tx, command=command())
    replay = skills.materialize_legacy_skills(
        tx, owner_id="owner", entries=(), manifest_digest=manifest
    )
    assert replay.replayed and replay.mappings == ()
    entry = LegacySkillEntry(
        uid(), "late", SkillDefinition("Late", "External file after cutover."), b"late file"
    )
    with pytest.raises(RepositoryConflictError):
        skills.materialize_legacy_skills(
            tx,
            owner_id="owner",
            entries=(entry,),
            manifest_digest=legacy_directory_digest("owner", (entry,)),
        )


def test_materialization_conflict_rolls_back_whole_batch(tx):
    skills = SkillsRepository()
    foreign = skills.apply_change(tx, command=command(owner_id="foreign"))
    entries = (
        LegacySkillEntry(
            uid(), "a", SkillDefinition("Good", "Good retained legacy text."), b"good"
        ),
        LegacySkillEntry(
            foreign.head.skill_id,
            "z",
            SkillDefinition("Conflict", "Conflict retained legacy text."),
            b"conflict",
        ),
    )
    with pytest.raises(RepositoryConflictError):
        skills.materialize_legacy_skills(
            tx,
            owner_id="owner",
            entries=entries,
            manifest_digest=legacy_directory_digest("owner", entries),
        )
    assert skills.list(tx, owner_id="owner") == ()
    assert skills.get_materialization(tx, owner_id="owner") is None


def test_note_current_ciphertext_replaced_no_value_history_and_minimal_forget(tx):
    notes = ExplicitNotesRepository()
    first = note(tx)
    changed = note(
        tx,
        note_id=first.note_id,
        expected_revision=1,
        category="goal",
        enabled=False,
        ciphertext=b"replacement-opaque",
    )
    assert changed.revision == 2 and changed.created_at == first.created_at
    assert notes.get_explicit_note(tx, owner_id="owner", note_id=first.note_id) == changed
    assert notes.get_explicit_note(tx, owner_id="foreign", note_id=first.note_id) is None
    assert notes.list_explicit_notes(tx, owner_id="owner", include_disabled=False) == ()
    tombstone = notes.forget_explicit_note(
        tx, owner_id="owner", note_id=first.note_id, expected_revision=2
    )
    assert type(tombstone) is ExplicitNoteTombstone and tombstone.revision == 3
    assert (
        notes.forget_explicit_note(tx, owner_id="owner", note_id=first.note_id, expected_revision=2)
        == tombstone
    )
    row = tx.fetch_one("SELECT * FROM explicit_note_current WHERE note_id=%s", (first.note_id,))
    assert {k for k, v in row.items() if v is not None} == {
        "owner_id",
        "note_id",
        "revision",
        "deleted_at",
        "deleted_reason",
    }
    assert notes.list_explicit_notes(tx, owner_id="owner") == ()
    with pytest.raises(RepositoryConflictError):
        note(tx, note_id=first.note_id, expected_revision=3)
    with pytest.raises(RepositoryNotFoundError):
        notes.forget_explicit_note(
            tx, owner_id="foreign", note_id=first.note_id, expected_revision=2
        )


def test_note_stale_or_changed_creation_metadata_refused_without_mutation(tx):
    notes = ExplicitNotesRepository()
    first = note(tx)
    prep = notes.prepare_explicit_note(
        tx, owner_id="owner", note_id=first.note_id, expected_revision=1
    )
    with pytest.raises(RepositoryConflictError):
        notes.put_explicit_note(
            tx,
            preparation=prep,
            record=replace(
                first, revision=2, created_at=first.created_at - 1, updated_at=prep.observed_at_ms
            ),
        )
    changed = note(tx, note_id=first.note_id, expected_revision=1, ciphertext=b"new")
    with pytest.raises(RepositoryConflictError):
        notes.put_explicit_note(
            tx, preparation=prep, record=replace(first, revision=2, updated_at=prep.observed_at_ms)
        )
    assert notes.get_explicit_note(tx, owner_id="owner", note_id=first.note_id) == changed


def expire_fixture(tx, record, offset=0):
    now = tx.fetch_one("SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint AS t")["t"]
    tx.execute(
        "UPDATE explicit_note_current SET created_at=%s,updated_at=%s,expires_at=%s "
        "WHERE note_id=%s",
        (now - 10000, now - 9000, now - 1000 + offset, record.note_id),
    )


def test_exact_expiry_is_unavailable_before_purge_and_erases_current_bytes(tx):
    notes = ExplicitNotesRepository()
    first = note(tx)
    expire_fixture(tx, first)
    assert notes.get_explicit_note(tx, owner_id="owner", note_id=first.note_id) is None
    assert notes.list_explicit_notes(tx, owner_id="owner") == ()
    with pytest.raises(RepositoryConflictError):
        notes.prepare_explicit_note(
            tx, owner_id="owner", note_id=first.note_id, expected_revision=1
        )
    page = notes.page_expired_explicit_notes(tx)
    assert len(page.records) == 1 and page.next_cursor is None
    gone = notes.expire_explicit_note(
        tx, owner_id="owner", note_id=first.note_id, expected_revision=1
    )
    assert gone.deleted_reason == "expired" and gone.revision == 2
    assert notes.page_expired_explicit_notes(tx).records == ()
    assert notes.get_explicit_note(tx, owner_id="owner", note_id=first.note_id) == gone


def test_expiry_page_has_finite_cutoff_and_stable_keyset(tx):
    notes = ExplicitNotesRepository()
    records = [note(tx, owner_id=f"owner-{i}") for i in range(3)]
    for r in records:
        expire_fixture(tx, r)
    first = notes.page_expired_explicit_notes(tx, limit=1)
    second = notes.page_expired_explicit_notes(
        tx, limit=1, after=first.next_cursor, cutoff_ms=first.cutoff_ms
    )
    third = notes.page_expired_explicit_notes(
        tx, limit=1, after=second.next_cursor, cutoff_ms=first.cutoff_ms
    )
    assert len({p.records[0].note_id for p in (first, second, third)}) == 3
    assert third.next_cursor is None
    assert {p.cutoff_ms for p in (first, second, third)} == {first.cutoff_ms}
    with pytest.raises(RepositoryValidationError):
        notes.page_expired_explicit_notes(tx, after=first.next_cursor)


def test_guidance_edit_retires_claim_and_unstarted_reservation_but_preserves_issued(tx, repo):
    from test_assignments_postgres import create_operation
    from test_operation_control_postgres import operation_claim
    from test_operation_payload_postgres import admission

    skills = SkillsRepository()
    initial = command()
    skills.apply_change(tx, command=initial)
    assignment = bind(
        repo, tx, create_operation(repo, tx), (GuidanceReference("skill", initial.skill_id, 1),)
    )
    running = operation_claim(repo, tx)
    _, _, binding = admission(repo, tx, running)
    issued_item = action(repo, tx, running.fence)
    permit = start(repo, tx, running.fence, reserve(repo, tx, running.fence, issued_item), binding)
    pending_item = action(repo, tx, running.fence)
    reserve(repo, tx, running.fence, pending_item)
    before = repo.get_assignment(tx, owner_id="owner", assignment_id=assignment.assignment_id)
    skills.apply_change(tx, command=edit(initial))
    after = repo.get_assignment(tx, owner_id="owner", assignment_id=assignment.assignment_id)
    assert after.lifecycle == "paused" and after.control_epoch == before.control_epoch + 1
    assert (
        tx.fetch_one(
            "SELECT data FROM persistent_assignment WHERE id=%s", (assignment.assignment_id,)
        )["data"]["claim_token"]
        is None
    )
    assert after.safe_error_code == "guidance_changed"
    assert after.usage["spent"] == before.usage["spent"]
    assert after.usage["outstanding"]["tool_calls"] == 1
    assert (
        repo.get_action(
            tx,
            owner_id="owner",
            assignment_id=assignment.assignment_id,
            action_id=pending_item.action_id,
        ).state
        == "invalidated"
    )
    with pytest.raises(RepositoryConflictError):
        control(repo, tx, after, "resume")
    settled = outcome(repo, tx, permit, assignment.assignment_id)
    assert settled.state == "succeeded" and settled.result["result"] == {}
    charged = repo.get_assignment(
        tx, owner_id="owner", assignment_id=assignment.assignment_id
    ).usage
    outcome(repo, tx, permit, assignment.assignment_id)
    assert (
        repo.get_assignment(tx, owner_id="owner", assignment_id=assignment.assignment_id).usage
        == charged
    )


def test_terminal_assignment_leaves_active_guidance_index(tx, repo):
    skills = SkillsRepository()
    initial = command()
    skills.apply_change(tx, command=initial)
    assignment = bind(
        repo, tx, create(repo, tx), (GuidanceReference("skill", initial.skill_id, 1),)
    )
    stopped = control(repo, tx, assignment, "stop").assignment
    assert stopped.lifecycle == "stopped"
    assert (
        tx.fetch_one(
            "SELECT active FROM assignment_guidance_reference WHERE assignment_id=%s",
            (assignment.assignment_id,),
        )["active"]
        is False
    )
    skills.apply_change(tx, command=edit(initial))
    assert (
        repo.get_assignment(tx, owner_id="owner", assignment_id=assignment.assignment_id) == stopped
    )


def test_disabled_foreign_or_missing_selection_rolls_back_entire_binding(tx, repo):
    skills = SkillsRepository()
    skill = skills.apply_change(tx, command=command(owner_id="foreign"))
    assignment = create(repo, tx)
    with pytest.raises(RepositoryConflictError):
        bind(repo, tx, assignment, (GuidanceReference("skill", skill.head.skill_id, 1),))
    assert tx.fetch_one("SELECT count(*) AS c FROM assignment_guidance_reference")["c"] == 0


def test_note_forget_invalidates_exact_original_reference_without_adopting_replacement(tx, repo):
    notes = ExplicitNotesRepository()
    first = note(tx)
    assignment = bind(repo, tx, create(repo, tx), (GuidanceReference("note", first.note_id, 1),))
    notes.forget_explicit_note(tx, owner_id="owner", note_id=first.note_id, expected_revision=1)
    current = repo.get_assignment(tx, owner_id="owner", assignment_id=assignment.assignment_id)
    assert current.lifecycle == "paused"
    with pytest.raises(RepositoryConflictError):
        control(repo, tx, current, "resume")
    ref = tx.fetch_one(
        "SELECT revision,invalidated_at FROM assignment_guidance_reference WHERE assignment_id=%s",
        (assignment.assignment_id,),
    )
    assert ref["revision"] == 1 and ref["invalidated_at"] is not None


def test_empty_initial_guidance_binding_cannot_later_select_nonempty(tx, repo):
    skills = SkillsRepository()
    skill = skills.apply_change(tx, command=command())
    assignment = bind(repo, tx, create(repo, tx), ())
    with pytest.raises(RepositoryConflictError):
        bind(repo, tx, assignment, (GuidanceReference("skill", skill.head.skill_id, 1),))
    assert tx.fetch_one("SELECT count(*) AS n FROM assignment_guidance_selection")["n"] == 1
    assert tx.fetch_one("SELECT count(*) AS n FROM assignment_guidance_reference")["n"] == 0


@pytest.mark.postgresql_lock_catalog
def test_skill_command_original_mutation_during_owner_wait_does_not_change_writes(database):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from test_assignment_execution_guard_postgres import _wait_for_lock
    from test_assignments_postgres import independent_database

    skills = SkillsRepository()
    value = command(owner_id="snapshot-owner", slug="snapshot")
    original = value.definition
    ready = Event()
    state = {}
    with database.transaction() as tx:
        schema = tx.fetch_one("SELECT current_schema() AS s")["s"]

    def run():
        with independent_database(schema) as db, db.transaction() as tx:
            state["waiter"] = tx.fetch_one("SELECT pg_backend_pid() AS p")["p"]
            ready.set()
            return skills.apply_change(tx, command=value)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.transaction() as tx:
            blocker = tx.fetch_one("SELECT pg_backend_pid() AS p")["p"]
            tx.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", (value.owner_id,))
            future = pool.submit(run)
            assert ready.wait(5)
            _wait_for_lock(tx, state["waiter"], blocker)
            object.__setattr__(
                value, "definition", SkillDefinition("Replaced", "Wrong post-wait content.")
            )
        result = future.result(5)
    assert result.revision.definition == original


def during_owner_wait(database, owner_id, run, change):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from test_assignment_execution_guard_postgres import _wait_for_lock
    from test_assignments_postgres import independent_database

    ready = Event()
    state = {}
    with database.transaction() as tx:
        schema = tx.fetch_one("SELECT current_schema() AS s")["s"]

    def worker():
        with independent_database(schema) as db, db.transaction() as tx:
            state["waiter"] = tx.fetch_one("SELECT pg_backend_pid() AS p")["p"]
            ready.set()
            return run(tx)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.transaction() as tx:
            blocker = tx.fetch_one("SELECT pg_backend_pid() AS p")["p"]
            tx.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", (owner_id,))
            future = pool.submit(worker)
            assert ready.wait(5)
            _wait_for_lock(tx, state["waiter"], blocker)
            change()
        return future.result(5)


@pytest.mark.postgresql_lock_catalog
def test_legacy_entry_mutation_during_owner_wait_cannot_change_captured_import(database):
    skills = SkillsRepository()
    value = LegacySkillEntry(
        uid(),
        "immutable",
        SkillDefinition("Original", "Original exact instructions."),
        b"original bytes",
    )
    raw = value.markdown
    manifest = legacy_directory_digest("legacy-snapshot", (value,))

    def change():
        object.__setattr__(value, "markdown", b"replaced")
        object.__setattr__(value.definition, "instructions", "Replacement definition after wait.")

    result = during_owner_wait(
        database,
        "legacy-snapshot",
        lambda tx: skills.materialize_legacy_skills(
            tx, owner_id="legacy-snapshot", entries=(value,), manifest_digest=manifest
        ),
        change,
    )
    with database.transaction() as tx:
        r = skills.get_revision(
            tx, owner_id="legacy-snapshot", skill_id=result.mappings[0].skill_id, revision=1
        )
    assert r.legacy_markdown == raw and r.definition.instructions == "Original exact instructions."


@pytest.mark.postgresql_lock_catalog
def test_note_preparation_mutation_during_owner_wait_cannot_replace_original_snapshot(database):
    notes = ExplicitNotesRepository()
    with database.transaction() as tx:
        first = note(tx, owner_id="note-snapshot")
        prep = notes.prepare_explicit_note(
            tx, owner_id=first.owner_id, note_id=first.note_id, expected_revision=1
        )
        record = replace(first, revision=2, updated_at=prep.observed_at_ms, ciphertext=b"successor")

    def change():
        object.__setattr__(prep.current, "ciphertext", b"wrong-original")
        object.__setattr__(prep, "owner_id", "another-owner")

    result = during_owner_wait(
        database,
        "note-snapshot",
        lambda tx: notes.put_explicit_note(tx, preparation=prep, record=record),
        change,
    )
    assert result == record


@pytest.mark.postgresql_lock_catalog
def test_guidance_reference_mutation_during_owner_wait_cannot_adopt_new_selection(database, repo):
    skills = SkillsRepository()
    with database.transaction() as tx:
        tx.execute("DELETE FROM assignment_operation_receipt")
        tx.execute("DELETE FROM persistent_assignment")
        initial = skills.apply_change(tx, command=command(slug="original-selection"))
        replacement = skills.apply_change(tx, command=command(slug="replacement-selection"))
        assignment = create(repo, tx)
        ref = GuidanceReference("skill", initial.head.skill_id, 1)
    result = during_owner_wait(
        database,
        "owner",
        lambda tx: bind(repo, tx, assignment, (ref,)),
        lambda: object.__setattr__(ref, "resource_id", replacement.head.skill_id),
    )
    with database.transaction() as tx:
        row = tx.fetch_one(
            "SELECT resource_id FROM assignment_guidance_reference WHERE assignment_id=%s",
            (result.assignment_id,),
        )
        assert str(row["resource_id"]) == initial.head.skill_id


@pytest.mark.parametrize(
    "operation",
    [
        lambda s, n, tx: s.get(tx, owner_id="owner", skill_id=uid(), include_deleted=1),
        lambda s, n, tx: s.list(tx, owner_id="owner", include_disabled=1),
        lambda s, n, tx: s.prepare_change(tx, command={}),
        lambda s, n, tx: s.materialize_legacy_skills(
            tx, owner_id="owner", entries=[], manifest_digest="a" * 64
        ),
        lambda s, n, tx: s.materialize_legacy_skills(
            tx, owner_id="owner", entries=(), manifest_digest="a" * 64
        ),
        lambda s, n, tx: n.get_explicit_note(
            tx, owner_id="owner", note_id=uid(), include_disabled=1
        ),
        lambda s, n, tx: n.list_explicit_notes(tx, owner_id="owner", include_disabled=1),
        lambda s, n, tx: n.put_explicit_note(tx, preparation=None, record=None),
        lambda s, n, tx: n.expire_explicit_note(
            tx, owner_id="owner", note_id=uid(), expected_revision=1, skip_locked=1
        ),
        lambda s, n, tx: n.page_expired_explicit_notes(tx, cutoff_ms=0, after={}),
    ],
)
def test_public_guidance_methods_refuse_ambiguous_arguments_before_mutation(tx, operation):
    with pytest.raises(RepositoryValidationError):
        operation(SkillsRepository(), ExplicitNotesRepository(), tx)
    assert tx.fetch_one("SELECT count(*) AS n FROM owner_skill_head")["n"] == 0
    assert tx.fetch_one("SELECT count(*) AS n FROM explicit_note_current")["n"] == 0


def test_note_pages_and_skill_history_cursor_preserve_current_owner_bounds(tx):
    notes = ExplicitNotesRepository()
    skills = SkillsRepository()
    a = note(tx)
    b = note(tx)
    first = notes.list_explicit_notes(tx, owner_id="owner", limit=1)[0]
    second = notes.list_explicit_notes(tx, owner_id="owner", after_id=first.note_id, limit=1)[0]
    assert {first.note_id, second.note_id} == {a.note_id, b.note_id}
    with pytest.raises(RepositoryConflictError):
        note(tx, owner_id="foreign", note_id=a.note_id)
    with pytest.raises(RepositoryConflictError):
        notes.expire_explicit_note(tx, owner_id="owner", note_id=a.note_id, expected_revision=1)
    with pytest.raises(RepositoryConflictError):
        notes.forget_explicit_note(tx, owner_id="owner", note_id=a.note_id, expected_revision=2)
    c = command()
    skills.apply_change(tx, command=c)
    skills.apply_change(tx, command=edit(c))
    assert [
        r.revision
        for r in skills.history(tx, owner_id="owner", skill_id=c.skill_id, before_revision=2)
    ] == [1]
    with pytest.raises(RepositoryConflictError):
        skills.materialize_legacy_skills(
            tx, owner_id="owner", entries=(), manifest_digest=legacy_directory_digest("owner", ())
        )


@pytest.mark.parametrize("form", ["list", "duplicate", "skills21", "notes9"])
def test_guidance_selection_bounds_are_enforced_atomically(tx, repo, form):
    assignment = create(repo, tx)
    one = GuidanceReference("skill", uid(), 1)
    refs = {
        "list": [one],
        "duplicate": (one, one),
        "skills21": tuple(GuidanceReference("skill", uid(), 1) for _ in range(21)),
        "notes9": tuple(GuidanceReference("note", uid(), 1) for _ in range(9)),
    }[form]
    with pytest.raises(RepositoryValidationError):
        repo.bind_guidance_references(
            tx,
            owner_id="owner",
            assignment_id=assignment.assignment_id,
            expected_instruction_revision=1,
            expected_control_epoch=1,
            expected_state_version=assignment.state_version,
            references=refs,
        )
    assert tx.fetch_one("SELECT count(*) AS n FROM assignment_guidance_selection")["n"] == 0


def guidance_assert(repo, tx, record):
    return repo.assert_guidance_current(
        tx,
        owner_id=record.owner_id,
        assignment_id=record.assignment_id,
        expected_instruction_revision=record.instruction_revision,
        expected_control_epoch=record.control_epoch,
        expected_state_version=record.state_version,
    )


def test_current_guidance_fact_replay_is_read_only_and_revision_fenced(tx, repo):
    initial = command()
    skills = SkillsRepository()
    skills.apply_change(tx, command=initial)
    assignment = bind(
        repo, tx, create(repo, tx), (GuidanceReference("skill", initial.skill_id, 1),)
    )
    assert guidance_assert(repo, tx, assignment) == assignment
    assert (
        bind(repo, tx, assignment, (GuidanceReference("skill", initial.skill_id, 1),)) == assignment
    )
    skills.apply_change(tx, command=edit(initial))
    current = repo.get_assignment(tx, owner_id="owner", assignment_id=assignment.assignment_id)
    with pytest.raises(RepositoryConflictError):
        guidance_assert(repo, tx, current)
    with pytest.raises(RepositoryConflictError):
        control(repo, tx, current, "revise", replacement=current.definition)


def test_guidance_cannot_bind_after_claim_even_when_no_resource_is_selected(tx, repo):
    assignment = create(repo, tx)
    claim(repo, tx)
    current = repo.get_assignment(tx, owner_id="owner", assignment_id=assignment.assignment_id)
    with pytest.raises(RepositoryConflictError):
        bind(repo, tx, current, ())


def test_expiry_refuses_claim_and_fact_without_requiring_purge(tx, repo):
    value = note(tx)
    now = tx.fetch_one("SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint AS t")["t"]
    value = note(tx, note_id=value.note_id, expected_revision=1, expires_at=now + 10000)
    assignment = bind(repo, tx, create(repo, tx), (GuidanceReference("note", value.note_id, 2),))
    assert guidance_assert(repo, tx, assignment) == assignment
    expire_fixture(tx, value)
    with pytest.raises(RepositoryConflictError):
        guidance_assert(repo, tx, assignment)
    with pytest.raises(RepositoryConflictError):
        claim(repo, tx)
    assert (
        repo.get_assignment(tx, owner_id="owner", assignment_id=assignment.assignment_id)
        == assignment
    )


def test_expiry_admin_skips_locked_owner_and_next_page_reaches_another_owner(database):
    from test_assignments_postgres import independent_database

    notes = ExplicitNotesRepository()
    with database.transaction() as tx:
        tx.execute("DELETE FROM explicit_note_current")
        a = note(tx, owner_id="expired-a")
        b = note(tx, owner_id="expired-b")
        expire_fixture(tx, a, offset=-500)
        expire_fixture(tx, b)
        schema = tx.fetch_one("SELECT current_schema() AS s")["s"]
    with independent_database(schema) as worker:
        with database.transaction() as held:
            held.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", (a.owner_id,))
            with worker.transaction() as tx:
                first = notes.page_expired_explicit_notes(tx, limit=1)
                assert first.records[0].note_id == a.note_id
                assert (
                    notes.expire_explicit_note(
                        tx,
                        owner_id=a.owner_id,
                        note_id=a.note_id,
                        expected_revision=1,
                        skip_locked=True,
                    )
                    is None
                )
                second = notes.page_expired_explicit_notes(
                    tx, limit=1, after=first.next_cursor, cutoff_ms=first.cutoff_ms
                )
                assert second.records[0].note_id == b.note_id
                expired = notes.expire_explicit_note(
                    tx,
                    owner_id=b.owner_id,
                    note_id=b.note_id,
                    expected_revision=1,
                    skip_locked=True,
                )
                if tx.dialect.is_sqlite:
                    assert expired is None
                else:
                    assert expired.deleted_reason == "expired"
        with worker.transaction() as tx:
            assert notes.page_expired_explicit_notes(tx).records[0].note_id == a.note_id
            assert (
                notes.expire_explicit_note(
                    tx,
                    owner_id=a.owner_id,
                    note_id=a.note_id,
                    expected_revision=1,
                    skip_locked=True,
                ).deleted_reason
                == "expired"
            )
            if tx.dialect.is_sqlite:
                assert (
                    notes.expire_explicit_note(
                        tx,
                        owner_id=b.owner_id,
                        note_id=b.note_id,
                        expected_revision=1,
                        skip_locked=True,
                    ).deleted_reason
                    == "expired"
                )


@pytest.mark.parametrize("reason", ["forgotten", "expired"])
def test_duplicate_retirements_prepare_one_hash_chained_audit(database, reason):
    from test_assignments_postgres import parallel_transactions
    from test_reconciliation_authority_postgres import append_audit

    notes = ExplicitNotesRepository()
    with database.transaction() as tx:
        value = note(tx, owner_id="retire-" + uid())
        if reason == "expired":
            expire_fixture(tx, value)
    args = dict(owner_id=value.owner_id, note_id=value.note_id, expected_revision=1)

    def retire(tx):
        prepared = notes.prepare_explicit_note_retirement(tx, reason=reason, **args)
        audit = None if prepared.replayed else append_audit(tx, value.note_id)
        result = (
            notes.forget_explicit_note if reason == "forgotten" else notes.expire_explicit_note
        )(tx, **args)
        return prepared, audit, result

    results = parallel_transactions(database, (retire, retire))
    assert sum(audit is not None for _, audit, _ in results) == 1
    assert sorted(p.replayed for p, _, _ in results) == [False, True]
    assert results[0][2] == results[1][2]


def test_note_retirement_and_real_audit_rollback_with_final_host_failure(database):
    from test_reconciliation_authority_postgres import append_audit

    from astralplane.repositories.audit import AuditRepository

    notes = ExplicitNotesRepository()
    with database.transaction() as tx:
        value = note(tx, owner_id="rollback-" + uid())
    args = dict(owner_id=value.owner_id, note_id=value.note_id, expected_revision=1)
    with pytest.raises(RuntimeError, match="final caller refusal"), database.transaction() as tx:
        notes.prepare_explicit_note_retirement(tx, reason="forgotten", **args)
        audit = append_audit(tx, value.note_id)
        notes.forget_explicit_note(tx, **args)
        raise RuntimeError("final caller refusal")
    with database.transaction() as tx:
        assert notes.get_explicit_note(tx, owner_id=value.owner_id, note_id=value.note_id) == value
        assert (
            AuditRepository().get(tx, chain_id=audit.event.chain_id, event_id=audit.event.event_id)
            is None
        )


def test_retirement_final_rechecks_revision_after_prepare(tx):
    notes = ExplicitNotesRepository()
    value = note(tx)
    args = dict(owner_id="owner", note_id=value.note_id, expected_revision=1)
    assert not notes.prepare_explicit_note_retirement(tx, reason="forgotten", **args).replayed
    newer = note(tx, note_id=value.note_id, expected_revision=1, ciphertext=b"new current only")
    with pytest.raises(RepositoryConflictError):
        notes.forget_explicit_note(tx, **args)
    assert notes.get_explicit_note(tx, owner_id="owner", note_id=value.note_id) == newer


def test_last_live_note_revision_can_always_forget_or_expire(tx):
    from astralplane.repositories.guidance_models import MAX_REVISION

    notes = ExplicitNotesRepository()
    for reason in ("forgotten", "expired"):
        value = note(tx)
        tx.execute(
            "UPDATE explicit_note_current SET revision=%s WHERE note_id=%s",
            (MAX_REVISION - 1, value.note_id),
        )
        if reason == "expired":
            expire_fixture(tx, value)
        result = (
            notes.forget_explicit_note if reason == "forgotten" else notes.expire_explicit_note
        )(tx, owner_id="owner", note_id=value.note_id, expected_revision=MAX_REVISION - 1)
        assert result.revision == MAX_REVISION and result.deleted_reason == reason


@pytest.mark.postgresql_lock_catalog
def test_owner_read_lock_serializes_note_page_against_mutation(database):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from test_assignment_execution_guard_postgres import _wait_for_lock
    from test_assignments_postgres import independent_database

    notes = ExplicitNotesRepository()
    ready = Event()
    state = {}
    with database.transaction() as tx:
        value = note(tx, owner_id="read-lock-" + uid())
        schema = tx.fetch_one("SELECT current_schema() AS s")["s"]

    def run():
        with independent_database(schema) as db, db.transaction() as tx:
            state["waiter"] = tx.fetch_one("SELECT pg_backend_pid() AS p")["p"]
            ready.set()
            return note(
                tx,
                owner_id=value.owner_id,
                note_id=value.note_id,
                expected_revision=1,
                ciphertext=b"new revision",
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.transaction() as tx:
            notes.lock_explicit_note_owner(tx, owner_id=value.owner_id)
            blocker = tx.fetch_one("SELECT pg_backend_pid() AS p")["p"]
            first = notes.list_explicit_notes(tx, owner_id=value.owner_id)
            future = pool.submit(run)
            assert ready.wait(5)
            _wait_for_lock(tx, state["waiter"], blocker)
            assert notes.list_explicit_notes(tx, owner_id=value.owner_id) == first
        assert future.result(5).revision == 2


def test_retired_owner_refuses_reads_and_new_mutation_but_forget_remains_possible(tx):
    skills = SkillsRepository()
    notes = ExplicitNotesRepository()
    value = note(tx)
    tx.execute(
        "INSERT INTO astralplane_blob_owner_state(owner_id,state,retired_at,updated_at) "
        "VALUES('owner','retired',clock_timestamp(),clock_timestamp())"
    )
    with pytest.raises(RepositoryConflictError):
        skills.lock_owner(tx, owner_id="owner")
    with pytest.raises(RepositoryConflictError):
        notes.lock_explicit_note_owner(tx, owner_id="owner")
    with pytest.raises(RepositoryConflictError):
        skills.apply_change(tx, command=command())
    assert (
        notes.forget_explicit_note(
            tx, owner_id="owner", note_id=value.note_id, expected_revision=1
        ).deleted_reason
        == "forgotten"
    )


def test_guidance_edit_retires_running_and_pending_tasks_without_losing_results(tx, repo):
    from astralplane.repositories.assignments import AssignmentTask, digest

    skills = SkillsRepository()
    initial = command()
    skills.apply_change(tx, command=initial)
    record = bind(repo, tx, create(repo, tx), (GuidanceReference("skill", initial.skill_id, 1),))
    selected = claim(repo, tx)
    tasks = tuple(
        AssignmentTask(key, "plan", 1, "Read", "Read source", record.definition.allowed_tools)
        for key in ("running", "pending")
    )
    repo.put_task_plan(
        tx,
        fence=selected.fence,
        expected_state_version=selected.assignment.state_version,
        plan_key="plan",
        plan_digest=digest(tasks),
        tasks=tasks,
    )
    old_task = repo.claim_task(
        tx, fence=selected.fence, task_id="running", expected_task_generation=0
    )
    before = repo.get_assignment(tx, owner_id="owner", assignment_id=record.assignment_id)
    skills.apply_change(tx, command=edit(initial))
    after = repo.get_assignment(tx, owner_id="owner", assignment_id=record.assignment_id)
    assert after.lifecycle == "paused" and after.control_epoch == before.control_epoch + 1
    assert all(task["state"] == "pending" for task in after.tasks)
    assert tuple(task["task_generation"] for task in after.tasks) == tuple(
        task["task_generation"] + 1 for task in before.tasks
    )
    from astralplane.repositories.assignments import AssignmentTaskResult

    with pytest.raises(RepositoryConflictError):
        repo.complete_task(
            tx, claim=old_task, result=AssignmentTaskResult("completed", digest("late"), "Late")
        )


def test_future_operation_guidance_invalidation_preserves_opaque_record_and_liabilities(tx, repo):
    from test_assignments_postgres import create_operation
    from test_operation_control_postgres import mutate

    initial = command()
    skills = SkillsRepository()
    skills.apply_change(tx, command=initial)
    record = bind(
        repo, tx, create_operation(repo, tx), (GuidanceReference("skill", initial.skill_id, 1),)
    )
    mutate(
        tx, record, lambda data: data["operation"].update(version=3, future={"opaque": "preserved"})
    )
    before = tx.fetch_one(
        "SELECT * FROM persistent_assignment WHERE id=%s", (record.assignment_id,)
    )
    skills.apply_change(tx, command=edit(initial))
    assert (
        tx.fetch_one("SELECT * FROM persistent_assignment WHERE id=%s", (record.assignment_id,))
        == before
    )
    assert (
        tx.fetch_one(
            "SELECT invalidated_at FROM assignment_guidance_reference WHERE assignment_id=%s",
            (record.assignment_id,),
        )["invalidated_at"]
        is not None
    )


def test_guidance_final_clock_observation_refuses_expired_selected_note(tx, repo, monkeypatch):
    from astralplane.repositories import guidance

    now = guidance._clock(tx)
    value = note(tx, expires_at=now + 10000)
    record = bind(repo, tx, create(repo, tx), (GuidanceReference("note", value.note_id, 1),))
    observations = iter((value.expires_at - 1, value.expires_at))
    monkeypatch.setattr(guidance, "_clock", lambda tx: next(observations))
    with pytest.raises(RepositoryConflictError, match="guidance_changed"):
        guidance_assert(repo, tx, record)


@pytest.mark.parametrize(
    "corruption", ["missing_header", "reference_revision", "instruction_revision"]
)
def test_guidance_header_and_immutable_selection_mismatches_fail_closed(tx, repo, corruption):
    from test_operation_control_postgres import mutate

    from astralplane.repositories import RepositoryDataError

    initial = command()
    SkillsRepository().apply_change(tx, command=initial)
    record = bind(repo, tx, create(repo, tx), (GuidanceReference("skill", initial.skill_id, 1),))
    if corruption == "missing_header":
        tx.execute(
            "DELETE FROM assignment_guidance_selection WHERE assignment_id=%s",
            (record.assignment_id,),
        )
    elif corruption == "reference_revision":
        tx.execute(
            "UPDATE assignment_guidance_reference SET revision=2 WHERE assignment_id=%s",
            (record.assignment_id,),
        )
    else:
        mutate(tx, record, lambda data: data.update(instruction_revision=2))
        record = repo.get_assignment(tx, owner_id="owner", assignment_id=record.assignment_id)
    with pytest.raises((RepositoryConflictError, RepositoryDataError)):
        guidance_assert(repo, tx, record)
    with pytest.raises((RepositoryConflictError, RepositoryDataError)):
        bind(repo, tx, record, (GuidanceReference("skill", initial.skill_id, 1),))


@pytest.mark.parametrize("kind", ["skill", "note"])
def test_caught_final_invalidation_failure_rolls_back_resource_claim_and_ledger(
    tx, repo, monkeypatch, kind
):
    from test_assignments_postgres import create_operation
    from test_operation_control_postgres import operation_claim
    from test_operation_payload_postgres import admission

    from astralplane.database.transaction import Transaction

    skills = SkillsRepository()
    notes = ExplicitNotesRepository()
    initial = command()
    selected_resource = (
        skills.apply_change(tx, command=initial).head if kind == "skill" else note(tx)
    )
    identity = initial.skill_id if kind == "skill" else selected_resource.note_id
    assignment = bind(repo, tx, create_operation(repo, tx), (GuidanceReference(kind, identity, 1),))
    selected = operation_claim(repo, tx)
    _, _, binding = admission(repo, tx, selected)
    item = action(repo, tx, selected.fence)
    permit = start(repo, tx, selected.fence, reserve(repo, tx, selected.fence, item), binding)
    tables = (
        "persistent_assignment",
        "persistent_assignment_action",
        "assignment_guidance_reference",
        "owner_skill_head",
        "owner_skill_revision",
        "explicit_note_current",
    )

    def snapshot():
        return {table: tx.fetch_all("SELECT * FROM " + table) for table in tables}

    before = snapshot()
    execute = Transaction.execute

    def fail_after_write(self, statement, parameters=()):
        result = execute(self, statement, parameters)
        if "UPDATE assignment_guidance_reference SET invalidated_at=" in statement:
            raise RuntimeError("controlled final invalidation failure")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Transaction, "execute", fail_after_write)
        with pytest.raises(RuntimeError, match="controlled final invalidation failure"):
            if kind == "skill":
                skills.apply_change(tx, command=edit(initial))
            else:
                notes.forget_explicit_note(
                    tx, owner_id="owner", note_id=identity, expected_revision=1
                )
    assert snapshot() == before
    assert outcome(repo, tx, permit, assignment.assignment_id).state == "succeeded"


def test_parallel_skill_receipt_and_competing_cas_keep_one_transition(database):
    from test_assignments_postgres import parallel_transactions

    from astralplane.repositories.guidance_models import SkillChangeResult

    skills = SkillsRepository()
    initial = command(owner_id="parallel-skill-" + uid())
    results = parallel_transactions(
        database, tuple(lambda tx: skills.apply_change(tx, command=initial) for _ in range(2))
    )
    assert all(type(result) is SkillChangeResult for result in results)
    assert sorted(result.replayed for result in results) == [False, True]
    updates = tuple(
        edit(initial, owner_id=initial.owner_id, definition=SkillDefinition("Update", text))
        for text in ("First proposed private revision.", "Second competing private revision.")
    )
    results = parallel_transactions(
        database, tuple(lambda tx, c=c: skills.apply_change(tx, command=c) for c in updates)
    )
    assert sum(isinstance(result, RepositoryConflictError) for result in results) == 1
    with database.transaction() as tx:
        assert len(skills.history(tx, owner_id=initial.owner_id, skill_id=initial.skill_id)) == 2
        assert skills.get(tx, owner_id=initial.owner_id, skill_id=initial.skill_id).revision == 2


def incomplete_legacy_fixture(tx, field):
    from astralplane.repositories.guidance_models import canonical

    skills = SkillsRepository()
    entry = LegacySkillEntry(
        uid(),
        "legacy-proof",
        SkillDefinition("Legacy", "Exact legacy definition."),
        b"legacy exact raw bytes",
        legacy_updated_at=7,
    )
    skills.materialize_legacy_skills(
        tx,
        owner_id="owner",
        entries=(entry,),
        manifest_digest=legacy_directory_digest("owner", (entry,)),
    )
    row = dict(
        tx.fetch_one("SELECT * FROM owner_skill_revision WHERE skill_id=%s", (entry.skill_id,))
    )
    row[field] = None
    tx.execute("DELETE FROM owner_skill_revision WHERE skill_id=%s", (entry.skill_id,))
    tx.execute(
        "INSERT INTO owner_skill_revision(owner_id,skill_id,revision,definition,definition_digest,"
        "created_at,deleted,legacy_markdown,legacy_digest,legacy_updated_at) "
        "VALUES(%s,%s,1,%s::jsonb,%s,%s,FALSE,%s,%s,%s)",
        (
            row["owner_id"],
            str(row["skill_id"]),
            canonical(dict(row["definition"])),
            row["definition_digest"],
            row["created_at"],
            bytes(row["legacy_markdown"]),
            row["legacy_digest"],
            row["legacy_updated_at"],
        ),
    )
    return entry


@pytest.mark.parametrize("field", ["legacy_digest", "legacy_updated_at"])
def test_nullable_legacy_proof_is_rejected_by_actual_constraint(tx, field):

    with check_violation(), tx.savepoint("incomplete_legacy_proof"):
        incomplete_legacy_fixture(tx, field)


@pytest.mark.postgresql_schema_catalog
def test_corrupt_legacy_timestamp_is_refused_by_public_read(tx):
    import re

    from astralplane.repositories import RepositoryDataError

    class RestoreFixtureError(Exception):
        pass

    with pytest.raises(RestoreFixtureError), tx.savepoint("corrupt_legacy_read"):
        constraints = tx.fetch_all(
            "SELECT conname FROM pg_constraint WHERE conrelid='owner_skill_revision'::regclass "
            "AND contype='c' AND pg_get_constraintdef(oid) LIKE '%%legacy_markdown%%'"
        )
        assert len(constraints) == 1
        name = constraints[0]["conname"]
        assert re.fullmatch(r"owner_skill_revision_[a-z0-9_]{1,40}", name)
        tx.execute('ALTER TABLE owner_skill_revision DROP CONSTRAINT "' + name + '"')
        entry = incomplete_legacy_fixture(tx, "legacy_updated_at")
        with pytest.raises(RepositoryDataError):
            SkillsRepository().get_revision(
                tx, owner_id="owner", skill_id=entry.skill_id, revision=1
            )
        raise RestoreFixtureError()
