"""Selected Work acceptance with real Plane/IAM/crypto and no provider calls."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from persistent_agents.models import AssignmentError
from persistent_agents.runtime_values import thaw
from tests.test_explicit_note_service_postgres_088 import (
    api as api, notes as notes, apply as apply_note,
)
from tests.test_declarative_agent_lifecycle_postgres_088 import research_definition
from tests.test_work_research_preflight_postgres_088 import (
    fixture as fixture, plane as plane, research_command,
    research_service as research_service, service as service,
    signing_key as signing_key, source_service as source_service,
)
from tests.test_work_submit_postgres_088 import context, totals

runtime = plane
pytestmark = pytest.mark.asyncio


def selected_command(service, **selected):
    body = json.loads(research_command(service))
    body["selection"] = {"version": 1, "agent": None, "skills": [], "notes": []} | selected
    return json.dumps(body).encode()


@pytest.mark.parametrize("kind", ["agent", "skills", "notes"])
async def test_missing_selected_head_cannot_be_silently_omitted(
    research_service, fixture, runtime, kind
):
    value = ({"agent_id": "absent-agent", "revision_id": str(uuid4())}
             if kind == "agent" else [{"skill_id" if kind == "skills" else "note_id":
                                      str(uuid4()), "revision": 1}])
    with pytest.raises(AssignmentError):
        await research_service.submit(
            await context(fixture, runtime), selected_command(research_service, **{kind: value}))
    assert totals(runtime, fixture[1]) == (0, 0, 0)


@pytest.fixture
async def selected_state(notes, monkeypatch):
    from astralplane.repositories.agent_models import DeclarativeAgentCommand
    from astralplane.repositories.guidance_models import SkillCommand, SkillDefinition
    from shared.feature_flags import flags
    monkeypatch.setitem(flags._flags, "byo_agents", True)
    notes.api.orch.explicit_notes = notes.service
    owner = notes.api.fixture[1]
    note = await apply_note(notes)
    with notes.api.runtime.transaction() as tx:
        skill = notes.api.runtime.repositories.preferences.skills.apply_change(tx, command=SkillCommand(
            owner, str(uuid4()), str(uuid4()), "create", 0, slug="selected-outline",
            definition=SkillDefinition("Selected outline", "Use precise attributed paragraphs.", ("always",))))
        agents = notes.api.runtime.repositories.agents
        create = DeclarativeAgentCommand(owner, "selected-agent-" + uuid4().hex,
            str(uuid4()), "create", revision_id=str(uuid4()), display_name="Private reader",
            definition=research_definition(notes))
        created = agents.apply_declarative_command(tx,
            preparation=agents.prepare_declarative_command(tx, command=create))
        activate = DeclarativeAgentCommand(owner, create.agent_id, str(uuid4()), "activate",
            expected_revision=created.agent.state_revision, revision_id=create.revision_id)
        active = agents.apply_declarative_command(tx,
            preparation=agents.prepare_declarative_command(tx, command=activate))
    return SimpleNamespace(notes=notes, api=notes.api, skill=skill.head, note=note,
        agent=active.agent, agent_revision=created.revision)


def selected_values(state, kind="all"):
    return {
        "agent": {"agent_id": state.agent.agent_id, "revision_id": state.agent_revision.revision_id}
                 if kind in {"all", "agent"} else None,
        "skills": [{"skill_id": state.skill.skill_id, "revision": state.skill.revision}]
                  if kind in {"all", "skills"} else [],
        "notes": [{"note_id": state.note.note_id, "revision": state.note.revision}]
                 if kind in {"all", "notes"} else [],
    }


@pytest.mark.parametrize("kind", ["agent", "skills", "notes", "all"])
async def test_exact_current_selection_binds_opaque_input_and_original_instruction_only(selected_state, kind):
    state = selected_state
    service = state.api.service
    body = selected_command(service, **selected_values(state, kind))
    accepted = await service.submit(await context(state.api.fixture, state.api.runtime), body)
    assert accepted.created and totals(state.api.runtime, state.api.fixture[1]) == (1, 1, 1)
    assert accepted.record.definition.instructions == json.loads(body)["instructions"]
    with state.api.runtime.transaction() as tx:
        captured = state.api.runtime.repositories.assignments.get_selected_input(tx,
            owner_id=state.api.fixture[1], assignment_id=accepted.record.assignment_id)
        durable = tx.fetch_one("SELECT selected_input FROM assignment_guidance_selection WHERE assignment_id=%s",
                              (accepted.record.assignment_id,))["selected_input"]
    assert captured.envelope is not None
    assert (captured.envelope.agent is not None) == (kind in {"agent", "all"})
    assert len(captured.references) == (2 if kind == "all" else 0 if kind == "agent" else 1)
    record_text = json.dumps(thaw(accepted.record)) + json.dumps(thaw(durable))
    assert "quoted sources" not in record_text and "attributed paragraphs" not in record_text
    assert "Keep quotations" not in record_text and "ciphertext" not in record_text
    events, _ = service.audit.list_for_user(state.api.fixture[1])
    accepted_event = next(event for event in events if event.action_type == "work.accept")
    assert accepted_event.inputs_meta["selection"] == {
        "version": 1, "skills": int(kind in {"skills", "all"}),
        "notes": int(kind in {"notes", "all"}), "agent_selected": kind in {"agent", "all"}}
    assert service.audit.verify_chain(state.api.fixture[1]) is None


async def test_accepted_retry_after_forget_never_reopens_values_or_revalidates_selection(selected_state, monkeypatch):
    from personalization.explicit_note_service import ExplicitNoteCommand
    from personalization.selected_guidance_boundary import SelectedGuidanceBoundary
    state = selected_state
    service = state.api.service
    raw = selected_command(service, **selected_values(state))
    accepted = await service.submit(await context(state.api.fixture, state.api.runtime), raw)
    await apply_note(state.notes, ExplicitNoteCommand("forget", state.note.note_id, 1))
    def forbidden(*args, **kwargs):
        pytest.fail("accepted replay must not re-open forgotten guidance")
    monkeypatch.setattr(SelectedGuidanceBoundary, "capture", forbidden)
    service.sessions.delete(state.api.fixture[2])
    replay = await service.submit(await context(state.api.fixture, state.api.runtime,
        cookie=False, bearer=True), raw)
    assert not replay.created and replay.record.assignment_id == accepted.record.assignment_id
    assert totals(state.api.runtime, state.api.fixture[1]) == (1, 1, 1)


async def test_absent_selection_preserves_no_header(selected_state):
    state = selected_state
    accepted = await state.api.service.submit(await context(state.api.fixture, state.api.runtime),
                                               research_command(state.api.service))
    with state.api.runtime.transaction() as tx:
        assert state.api.runtime.repositories.assignments.get_selected_input(tx,
            owner_id=state.api.fixture[1], assignment_id=accepted.record.assignment_id) is None


@pytest.mark.parametrize("kind", ["skill", "note", "agent", "session"])
async def test_retirement_after_exact_capture_cannot_adopt_new_heads(selected_state, monkeypatch, kind):
    from astralplane.repositories.agent_models import DeclarativeAgentCommand
    from astralplane.repositories.guidance_models import SkillCommand, SkillDefinition
    from personalization.explicit_note_service import ExplicitNoteCommand
    state = selected_state
    service = state.api.service
    original = service._prepare_selection
    async def retire(*args, **kwargs):
        captured = await original(*args, **kwargs)
        if kind == "session":
            service.sessions.delete(state.api.fixture[2])
        elif kind == "note":
            await apply_note(state.notes, ExplicitNoteCommand("forget", state.note.note_id, 1))
        else:
            with state.api.runtime.transaction() as tx:
                if kind == "skill":
                    state.api.runtime.repositories.preferences.skills.apply_change(tx, command=SkillCommand(
                        state.api.fixture[1], state.skill.skill_id, str(uuid4()), "replace", 1,
                        definition=SkillDefinition("New selected outline", "Use a changed paragraph order.", ("always",))))
                else:
                    repo = state.api.runtime.repositories.agents
                    command = DeclarativeAgentCommand(state.api.fixture[1], state.agent.agent_id,
                        str(uuid4()), "archive", expected_revision=state.agent.state_revision)
                    repo.apply_declarative_command(tx, preparation=repo.prepare_declarative_command(tx, command=command))
        return captured
    monkeypatch.setattr(service, "_prepare_selection", retire)
    with pytest.raises(AssignmentError):
        await service.submit(await context(state.api.fixture, state.api.runtime),
                             selected_command(service, **selected_values(state)))
    assert totals(state.api.runtime, state.api.fixture[1]) == (0, 0, 0)


@pytest.mark.parametrize("failure", ["audit", "key", "notes_service", "context_expiry", "agent_flag", "assignment_service"])
async def test_late_audit_or_identity_failure_rolls_back_all_selection_rows(selected_state, monkeypatch, failure):
    from datetime import datetime, timezone, timedelta
    state = selected_state
    service = state.api.service
    caller = await context(state.api.fixture, state.api.runtime)
    original = service.audit.insert_in_transaction
    def audit(event, **kwargs):
        saved = original(event, **kwargs)
        if event.action_type == "work.accept":
            if failure == "audit":
                raise RuntimeError("synthetic audit failure after actual insert")
            if failure == "key":
                monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-retired-key-" + "z" * 40)
            elif failure == "notes_service":
                state.api.orch.explicit_notes = object()
            elif failure == "context_expiry":
                object.__setattr__(caller, "principal_expires_at", datetime.now(timezone.utc) - timedelta(seconds=1))
            elif failure == "agent_flag":
                from shared.feature_flags import flags
                monkeypatch.setitem(flags._flags, "byo_agents", False)
            elif failure == "assignment_service":
                state.api.orch.persistent_assignments = object()
        return saved
    monkeypatch.setattr(service.audit, "insert_in_transaction", audit)
    with pytest.raises((AssignmentError, RuntimeError)):
        await service.submit(caller, selected_command(service, **selected_values(state)))
    assert totals(state.api.runtime, state.api.fixture[1]) == (0, 0, 0)
    with state.api.runtime.transaction() as tx:
        assert tx.fetch_one("SELECT count(*) AS n FROM assignment_guidance_selection")["n"] == 0
        assert tx.fetch_one("SELECT count(*) AS n FROM assignment_guidance_reference")["n"] == 0
        assert tx.fetch_one("SELECT count(*) AS n FROM assignment_selected_agent")["n"] == 0


async def test_captured_inputs_compose_without_any_later_row_lookup(selected_state, monkeypatch):
    from personalization.selected_guidance_boundary import SelectedGuidanceBoundary
    from llm_config.tests.test_research_profile_088 import retained
    from personalization.selected_guidance import SelectedGuidanceUnavailable
    state = selected_state
    captures = []
    original = SelectedGuidanceBoundary.capture
    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        captures.append(result)
        return result
    monkeypatch.setattr(SelectedGuidanceBoundary, "capture", capture)
    await state.api.service.submit(await context(state.api.fixture, state.api.runtime),
        selected_command(state.api.service, **selected_values(state)))
    captured = captures[-1]
    def forbidden(*args, **kwargs):
        pytest.fail("compose must reuse exact captured rows")
    monkeypatch.setattr(state.api.runtime.repositories.preferences.skills, "get", forbidden)
    first = captured.compose(observation=retained(), approved_request_bytes=65536)
    later = captured.compose(observation=retained("A changed public page."), approved_request_bytes=65536)
    assert first.envelope == later.envelope == captured.prepared.envelope
    assert first.request.body != later.request.body
    assert "quoted sources" not in repr(captured)
    with pytest.raises(TypeError):
        captured._inputs["instruction"] = "adopted text"
    object.__setattr__(captured._inputs["skills"][0], "revision", 2)
    with pytest.raises(SelectedGuidanceUnavailable):
        captured.compose(observation=retained(), approved_request_bytes=65536)


@pytest.mark.parametrize("outcome", ["sensitive", "invalid", "unavailable"])
async def test_selected_prose_must_pass_existing_privacy_gate_outside_sql(selected_state, monkeypatch, outcome):
    state = selected_state
    service = state.api.service
    original = service.assignments.phi_gate.contains_phi
    def policy(text):
        if "owner_guidance" in text or "Owner-stated guidance" in text:
            if outcome == "invalid":
                raise ValueError("synthetic invalid privacy input")
            if outcome == "unavailable":
                raise RuntimeError("synthetic privacy outage")
            return True
        return original(text)
    monkeypatch.setattr(service.assignments.phi_gate, "contains_phi", policy)
    with pytest.raises(AssignmentError) as error:
        await service.submit(await context(state.api.fixture, state.api.runtime),
                             selected_command(service, **selected_values(state)))
    assert error.value.status_code == (503 if outcome == "unavailable" else 422)
    assert totals(state.api.runtime, state.api.fixture[1]) == (0, 0, 0)


@pytest.mark.parametrize("kind", ["agent", "skills", "notes"])
async def test_current_owner_cannot_adopt_absent_or_other_revision(selected_state, kind):
    state = selected_state
    values = selected_values(state)
    if kind == "agent":
        values[kind]["revision_id"] = str(uuid4())
    else:
        values[kind][0]["revision"] += 1
    with pytest.raises(AssignmentError) as error:
        await state.api.service.submit(await context(state.api.fixture, state.api.runtime),
                                       selected_command(state.api.service, **values))
    assert error.value.status_code == 404
    assert totals(state.api.runtime, state.api.fixture[1]) == (0, 0, 0)


async def test_current_note_ciphertext_must_authenticate_before_work_creation(selected_state):
    state = selected_state
    with state.api.runtime.transaction() as tx:
        # Test-owned storage corruption, not a product mutation path.
        tx.execute("UPDATE explicit_note_current SET ciphertext=%s WHERE note_id=%s",
                   ("synthetic-invalid-ciphertext", state.note.note_id))
    with pytest.raises(AssignmentError) as error:
        await state.api.service.submit(await context(state.api.fixture, state.api.runtime),
            selected_command(state.api.service, **selected_values(state)))
    assert (error.value.code, error.value.status_code) == ("work_selection_unavailable", 503)
    assert totals(state.api.runtime, state.api.fixture[1]) == (0, 0, 0)


@pytest.mark.parametrize("refusal", ["flag", "capability", "budget", "step_timeout"])
async def test_active_neutral_agent_pointer_does_not_grant_host_profile_or_budget(selected_state, monkeypatch, refusal):
    from astralplane.repositories.agent_models import DeclarativeAgentCommand
    from shared.feature_flags import flags
    state = selected_state
    if refusal == "flag":
        monkeypatch.setitem(flags._flags, "byo_agents", False)
    else:
        value = research_definition(state.notes)
        if refusal == "capability":
            value["capabilities"] = []
        elif refusal == "budget":
            value["limits"]["daily"]["tokens"] = 100
        else:
            value["limits"]["step_timeout_ms"] = 1000
        with state.api.runtime.transaction() as tx:
            repo = state.api.runtime.repositories.agents
            command = DeclarativeAgentCommand(state.api.fixture[1], state.agent.agent_id,
                str(uuid4()), "revise", expected_revision=state.agent.state_revision,
                revision_id=str(uuid4()), parent_revision_id=state.agent_revision.revision_id,
                display_name="Selected revision", definition=value)
            revised = repo.apply_declarative_command(tx,
                preparation=repo.prepare_declarative_command(tx, command=command))
            activated = DeclarativeAgentCommand(state.api.fixture[1], state.agent.agent_id,
                str(uuid4()), "activate", expected_revision=revised.agent.state_revision,
                revision_id=revised.revision.revision_id)
            repo.apply_declarative_command(tx, preparation=repo.prepare_declarative_command(tx, command=activated))
            state.agent_revision = revised.revision
    with pytest.raises(AssignmentError):
        await state.api.service.submit(await context(state.api.fixture, state.api.runtime),
            selected_command(state.api.service, **selected_values(state)))
    assert totals(state.api.runtime, state.api.fixture[1]) == (0, 0, 0)


@pytest.mark.parametrize("kind", ["skills", "notes"])
async def test_last_selected_query_uses_original_cutoff_and_database_clock(selected_state, monkeypatch, kind):
    from dataclasses import replace
    from datetime import datetime, timezone, timedelta
    import time
    from orchestrator import work_submit
    state = selected_state
    original_refresh = work_submit.refresh_work_submission_authority
    async def short_observation(*args, **kwargs):
        result = await original_refresh(*args, **kwargs)
        return replace(result, observation=replace(result.observation,
            valid_until=datetime.now(timezone.utc) + timedelta(seconds=2)))
    monkeypatch.setattr(work_submit, "refresh_work_submission_authority", short_observation)
    captured_now = datetime.now(timezone.utc)
    class LaggingHostClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return captured_now
    monkeypatch.setattr(work_submit, "datetime", LaggingHostClock)
    repo = state.api.runtime.repositories.assignments
    original_final = repo.assert_selected_input_current
    def final_query(tx, **kwargs):
        time.sleep(2.2)
        return original_final(tx, **kwargs)
    monkeypatch.setattr(repo, "assert_selected_input_current", final_query)
    with pytest.raises(AssignmentError):
        await state.api.service.submit(await context(state.api.fixture, state.api.runtime),
            selected_command(state.api.service, **selected_values(state, kind)))
    assert totals(state.api.runtime, state.api.fixture[1]) == (0, 0, 0)


async def test_same_revision_cipher_replacement_cannot_match_captured_binding(selected_state, monkeypatch):
    state = selected_state
    service = state.api.service
    original = service._prepare_selection
    async def reseal(*args, **kwargs):
        captured = await original(*args, **kwargs)
        with state.api.runtime.transaction() as tx:
            row = state.api.runtime.repositories.preferences.personalization.get_explicit_note(
                tx, owner_id=state.api.fixture[1], note_id=state.note.note_id)
            encrypted = state.notes.service._encrypted(row)
            new = state.notes.service.cipher.seal(encrypted.metadata, "A different private owner preference.")
            # Simulated corruption bypassing the revisioned public API. The
            # authenticated envelope must still reject an unchanged row ID/rev.
            tx.execute("UPDATE explicit_note_current SET ciphertext=%s WHERE note_id=%s",
                       (new.ciphertext, state.note.note_id))
        return captured
    monkeypatch.setattr(service, "_prepare_selection", reseal)
    with pytest.raises(AssignmentError) as error:
        await service.submit(await context(state.api.fixture, state.api.runtime),
                             selected_command(service, **selected_values(state)))
    assert (error.value.code, error.value.status_code) == ("work_selection_changed", 409)
    assert totals(state.api.runtime, state.api.fixture[1]) == (0, 0, 0)


@pytest.mark.parametrize("retirement", ["key", "notes_service", "agent_flag"])
async def test_final_selected_query_wait_cannot_outlive_local_key_service_or_agent_policy(
    selected_state, monkeypatch, retirement
):
    import asyncio
    import threading
    from shared.feature_flags import flags
    state = selected_state
    entered, release = threading.Event(), threading.Event()
    repo = state.api.runtime.repositories.assignments
    original = repo.assert_selected_input_current
    def held_final(tx, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(tx, **kwargs)
    monkeypatch.setattr(repo, "assert_selected_input_current", held_final)
    caller = await context(state.api.fixture, state.api.runtime)
    task = asyncio.create_task(state.api.service.submit(caller,
        selected_command(state.api.service, **selected_values(state))))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        if retirement == "key":
            monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-retired-final-key-" + "q" * 40)
        elif retirement == "notes_service":
            state.api.orch.explicit_notes = object()
        else:
            monkeypatch.setitem(flags._flags, "byo_agents", False)
        release.set()
        with pytest.raises(AssignmentError):
            await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert totals(state.api.runtime, state.api.fixture[1]) == (0, 0, 0)


async def test_config_wait_holds_owner_selection_fence_until_acceptance_commits(selected_state, monkeypatch):
    import asyncio
    import threading
    import time
    from astralplane.repositories.guidance_models import SkillCommand, SkillDefinition
    from orchestrator.work_submit import FixedResearchPreflight
    state = selected_state
    runtime, service, owner = state.api.runtime, state.api.service, state.api.fixture[1]
    in_config, writer_started = threading.Event(), threading.Event()
    writer_pid = []
    original = FixedResearchPreflight.assert_current
    def config(preflight, *args, **kwargs):
        if preflight is service.research_preflight:
            in_config.set()
        return original(preflight, *args, **kwargs)
    monkeypatch.setattr(FixedResearchPreflight, "assert_current", config)
    def change_skill():
        with runtime.transaction() as tx:
            writer_pid.append(tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"])
            writer_started.set()
            return runtime.repositories.preferences.skills.apply_change(tx, command=SkillCommand(
                owner, state.skill.skill_id, str(uuid4()), "replace", 1,
                definition=SkillDefinition("Changed outline", "Use the successor attribution layout.", ("always",))))
    caller = await context(state.api.fixture, runtime)
    blocker = runtime.transaction()
    held = blocker.__enter__()
    runtime.repositories.encrypted_llm_config.get_user_for_update(held, owner_id=owner)
    work = asyncio.create_task(service.submit(caller, selected_command(service, **selected_values(state))))
    writer = None
    try:
        assert await asyncio.to_thread(in_config.wait, 3)
        writer = asyncio.create_task(asyncio.to_thread(change_skill))
        assert await asyncio.to_thread(writer_started.wait, 2)
        until = time.monotonic() + 0.5
        blocked = False
        while time.monotonic() < until:
            with runtime.transaction() as tx:
                row = tx.fetch_one("SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (writer_pid[0],))
            if row["wait_event_type"] == "Lock":
                blocked = True
                break
            await asyncio.sleep(0.01)
        assert blocked and not writer.done() and not work.done()
    finally:
        blocker.__exit__(None, None, None)
        await asyncio.gather(work, *([] if writer is None else [writer]), return_exceptions=True)
    accepted = await work
    changed = await writer
    assert accepted.created and changed.head.revision == 2
    assert totals(runtime, owner) == (1, 1, 1)
    with runtime.transaction() as tx:
        record = runtime.repositories.assignments.get_assignment(tx, owner_id=owner, assignment_id=accepted.record.assignment_id)
    assert record.lifecycle == "paused" and record.safe_error_code == "guidance_changed"
