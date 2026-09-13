"""Private notes over real IAM, encryption, Plane CAS and required atomic audit.

The external JWT responses and local PHI analyzer result are synthetic. No model
or network effect starts; raw PostgreSQL reads below are test-only diagnostics.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
import threading
import time
from uuid import uuid4

from cryptography.fernet import Fernet
import pytest

from orchestrator.credential_manager import CredentialManager
from orchestrator.human_request_authority import HumanRequestBoundary, authenticate_current_human_request
from personalization.explicit_note_service import ExplicitNoteCommand, ExplicitNoteService
from personalization import phi_gate
from persistent_agents.models import AssignmentError
from tests.test_request_session_authority_088 import request
from tests.test_work_admission_api_postgres_088 import (
    api as api, fixture as fixture, plane as plane, research_service as research_service,
    service as service, signing_key as signing_key, source_service as source_service,
)

runtime = plane
pytestmark = pytest.mark.asyncio


async def test_delivery_snapshot_rechecks_exact_displayed_values_after_render(notes):
    first = await apply(notes)
    read = await caller(notes, method="GET")
    displayed = await notes.service.get(caller=read, note_id=first.note_id)
    assert await notes.service.verify_snapshot(caller=read, notes=(displayed,)) is None
    await apply(notes, body(note_id=first.note_id, expected_revision=1, value="A corrected note value."))
    with pytest.raises(AssignmentError) as stale:
        await notes.service.verify_snapshot(caller=await caller(notes, method="GET"), notes=(displayed,))
    assert stale.value.status_code == 409


async def test_delivery_snapshot_does_not_adopt_forget_during_final_identity_wait(notes, monkeypatch):
    first = await apply(notes)
    read = await caller(notes, method="GET")
    displayed = await notes.service.get(caller=read, note_id=first.note_id)
    original = type(read).verify_delivery
    async def retire_before_return(captured):
        if captured is read:
            await apply(notes, ExplicitNoteCommand(command="forget", note_id=first.note_id, expected_revision=1))
        await original(captured)
    monkeypatch.setattr(type(read), "verify_delivery", retire_before_return)
    with pytest.raises(AssignmentError):
        await notes.service.verify_snapshot(caller=read, notes=(displayed,))


async def test_delivery_snapshot_is_typed_bounded_owner_bound_and_value_exact(notes):
    first = await apply(notes)
    read = await caller(notes, method="GET")
    displayed = await notes.service.get(caller=read, note_id=first.note_id)
    for value in ([displayed], (displayed,) * 101, (object(),),
                  (replace(displayed, value="fabricated replacement"),),
                  (replace(displayed, metadata=replace(displayed.metadata, owner_id="another-owner")),)):
        with pytest.raises(AssignmentError):
            await notes.service.verify_snapshot(caller=read, notes=value)
    assert await notes.service.verify_snapshot(caller=read, notes=()) is None


@pytest.fixture
async def notes(api, monkeypatch, tmp_path):
    monkeypatch.setattr(phi_gate, "_GATE", phi_gate.PHIGate(
        analyzer=SimpleNamespace(analyze=lambda **_: [])))
    api.orch.credential_manager = CredentialManager(plane_runtime=api.runtime, data_dir=str(tmp_path))
    boundary = HumanRequestBoundary(api.orch)
    api.orch.human_request_boundary = boundary
    result = SimpleNamespace(api=api, boundary=boundary, service=ExplicitNoteService(api.orch))
    try:
        yield result
    finally:
        boundary.close()
        api.service.store.async_runtime.close()


async def caller(state, *, method="POST", cookie=True, owner=None):
    fixture = state.api.fixture
    token = fixture[3](**({"sub": owner} if owner else {}))
    incoming = request(fixture[2] if cookie else None, method=method, headers=[
        (b"authorization", ("Bearer " + token).encode()),
        (b"content-type", b"application/json"), (b"origin", b"https://app.invalid"),
    ])
    incoming.scope["app"] = state.api.app
    return await authenticate_current_human_request(incoming, boundary=state.boundary)


def body(**changes):
    return replace(ExplicitNoteCommand(command="save", note_id=str(uuid4()), expected_revision=0,
        category="preference", value="Use concise paragraphs and quoted sources.", enabled=True), **changes)


async def apply(state, command=None, **changes):
    return await state.service.command(caller=await caller(state), body=command or body(**changes))


def rows(state):
    with state.api.runtime.transaction() as tx:
        return tx.fetch_all("SELECT * FROM explicit_note_current ORDER BY note_id")


def events(state):
    with state.api.runtime.transaction() as tx:
        return tx.fetch_all("SELECT * FROM audit_events WHERE action_type LIKE 'explicit_note_%'")


async def test_encrypted_create_correct_disable_enable_and_search(notes):
    intent = body()
    first = await apply(notes, intent)
    first_cipher = bytes(rows(notes)[0]["ciphertext"])
    assert intent.value.encode() not in first_cipher and not hasattr(first, "value")
    opened = await notes.service.get(caller=await caller(notes, method="GET"), note_id=first.note_id)
    assert opened.value == intent.value and opened.metadata == first
    second = await apply(notes, replace(intent, expected_revision=1, value="Answer briefly in French."))
    second_cipher = bytes(rows(notes)[0]["ciphertext"])
    assert second.revision == 2 and second.created_at == first.created_at
    assert second_cipher != first_cipher
    disabled = await apply(notes, ExplicitNoteCommand(command="set_enabled", note_id=first.note_id,
                                                    expected_revision=2, enabled=False))
    page = await notes.service.list(caller=await caller(notes, method="GET"), search="french")
    assert len(page.notes) == 1 and page.notes[0].metadata == disabled and not disabled.enabled
    enabled = await apply(notes, ExplicitNoteCommand(command="set_enabled", note_id=first.note_id,
                                                   expected_revision=3, enabled=True))
    assert enabled.revision == 4 and enabled.enabled
    assert len(rows(notes)) == 1 and len(events(notes)) == 4
    assert all(intent.value not in str(event) and "French" not in str(event) for event in events(notes))
    assert notes.api.service.audit.verify_chain(notes.api.fixture[1]) is None


async def test_revision_conflict_and_cross_owner_never_overwrite(notes):
    intent = body()
    await apply(notes, intent)
    before = rows(notes)
    with pytest.raises(AssignmentError) as stale:
        await apply(notes, intent)
    assert stale.value.status_code == 409
    other = await caller(notes, cookie=False, owner="another-owner")
    with pytest.raises(AssignmentError):
        await notes.service.command(caller=other, body=replace(intent, expected_revision=1))
    with pytest.raises(AssignmentError) as hidden:
        await notes.service.get(caller=await caller(notes, cookie=False, owner="another-owner", method="GET"),
                                note_id=intent.note_id)
    assert hidden.value.status_code == 404
    assert rows(notes) == before and len(events(notes)) == 1


async def test_corrupt_ciphertext_refuses_reads_and_correction_but_can_forget(notes):
    intent = body()
    await apply(notes, intent)
    with notes.api.runtime.transaction() as tx:
        tx.execute("UPDATE explicit_note_current SET ciphertext=%s WHERE note_id=%s",
                   (b"unreadable ciphertext", intent.note_id))
    for action in (lambda: notes.service.get(caller=selected, note_id=intent.note_id),
                   lambda: notes.service.command(caller=selected, body=replace(intent, expected_revision=1))):
        selected = await caller(notes)
        with pytest.raises(AssignmentError):
            await action()
    forgotten = await apply(notes, ExplicitNoteCommand(command="forget", note_id=intent.note_id,
                                                     expected_revision=1))
    assert forgotten.revision == 2 and forgotten.deleted_reason == "forgotten"
    row = rows(notes)[0]
    assert all(row[key] is None for key in ("ciphertext", "category", "enabled", "created_at",
                                          "updated_at", "expires_at", "format_version"))
    replay = await apply(notes, ExplicitNoteCommand(command="forget", note_id=intent.note_id,
                                                  expected_revision=1))
    assert replay == forgotten and len(events(notes)) == 2


@pytest.mark.parametrize("mutation", ["audit_error", "audit_false", "key", "credentials", "notes", "audit"])
async def test_required_audit_and_same_composition_rollback(notes, monkeypatch, mutation):
    from audit.repository import AuditRepository
    original = AuditRepository.insert_in_transaction

    def changed(self, event, **kwargs):
        if mutation == "audit_error":
            raise RuntimeError("synthetic audit failure")
        saved = original(self, event, **kwargs)
        if mutation == "audit_false":
            return None
        if mutation == "key":
            notes.api.orch.credential_manager._fernet = Fernet(Fernet.generate_key())
        if mutation == "credentials":
            notes.api.orch.credential_manager = object()
        if mutation == "notes":
            notes.api.runtime.repositories.preferences.personalization = object()
        if mutation == "audit":
            notes.api.orch.audit_repo = object()
        return saved

    monkeypatch.setattr(AuditRepository, "insert_in_transaction", changed)
    with pytest.raises(AssignmentError):
        await apply(notes)
    assert not rows(notes) and not events(notes)


@pytest.mark.parametrize("privacy", ["positive", "unavailable", "exception"])
async def test_phi_failure_creates_no_current_or_audit_row(notes, monkeypatch, privacy):
    if privacy == "unavailable":
        gate = phi_gate.PHIGate(build_if_missing=False)
    elif privacy == "exception":
        def fail(_):
            raise RuntimeError("synthetic detector failure")
        gate = SimpleNamespace(contains_phi=fail)
    else:
        gate = SimpleNamespace(contains_phi=lambda _: True)
    monkeypatch.setattr(phi_gate, "_GATE", gate)
    with pytest.raises(AssignmentError):
        await apply(notes)
    assert not rows(notes) and not events(notes)


async def test_body_is_frozen_across_privacy_wait(notes, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    examined = []

    def scan(value):
        examined.append(value)
        entered.set()
        assert release.wait(3)
        return False

    monkeypatch.setattr(phi_gate, "_GATE", SimpleNamespace(contains_phi=scan))
    intent = body()
    selected = await caller(notes)
    task = asyncio.create_task(notes.service.command(caller=selected, body=intent))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        object.__setattr__(intent, "value", "Unscanned replacement")
        object.__setattr__(intent, "note_id", str(uuid4()))
    finally:
        release.set()
    result = await task
    opened = await notes.service.get(caller=await caller(notes, method="GET"), note_id=result.note_id)
    assert opened.value == examined[0] and result.note_id != intent.note_id


async def test_revocation_during_privacy_wait_creates_nothing(notes, monkeypatch):
    selected = await caller(notes)
    monkeypatch.setattr(phi_gate, "_GATE", SimpleNamespace(contains_phi=lambda _:
        (notes.api.fixture[0].delete(notes.api.fixture[2]), False)[1]))
    with pytest.raises(AssignmentError):
        await notes.service.command(caller=selected, body=body())
    assert not rows(notes) and not events(notes)


async def test_read_transport_cannot_write(notes):
    with pytest.raises(AssignmentError) as refusal:
        await notes.service.command(caller=await caller(notes, method="GET"), body=body())
    assert refusal.value.status_code == 403 and not rows(notes)


@pytest.mark.parametrize("method", ["get", "list"])
async def test_changed_value_during_delivery_is_not_adopted(notes, monkeypatch, method):
    from orchestrator.human_request_authority import CurrentHumanCaller
    intent = body()
    await apply(notes, intent)
    selected = await caller(notes, method="GET")
    original = CurrentHumanCaller.verify_delivery

    async def delivery(self):
        await original(self)
        if self is selected:
            await apply(notes, replace(intent, expected_revision=1, value="Changed during delivery."))

    monkeypatch.setattr(CurrentHumanCaller, "verify_delivery", delivery)
    with pytest.raises(AssignmentError) as refusal:
        if method == "get":
            await notes.service.get(caller=selected, note_id=intent.note_id)
        else:
            await notes.service.list(caller=selected)
    assert refusal.value.code == "explicit_note_changed"


async def test_search_cursor_advances_scanned_nonmatching_rows(notes):
    identities = sorted(str(uuid4()) for _ in range(3))
    for index, identity in enumerate(identities):
        await apply(notes, note_id=identity, value="Brief replies" if index == 2 else "Use paragraphs")
    first = await notes.service.list(caller=await caller(notes, method="GET"), limit=2, search="BRIEF")
    assert not first.notes and first.next_cursor == identities[1]
    second = await notes.service.list(caller=await caller(notes, method="GET"), limit=2,
                                     after_id=first.next_cursor, search="BRIEF")
    assert len(second.notes) == 1 and second.notes[0].metadata.note_id == identities[2]
    assert second.next_cursor is None


async def test_expiry_is_unavailable_before_physical_erasure(notes):
    intent = body(expires_at=time.time_ns()//1_000_000 + 400)
    await apply(notes, intent)
    await asyncio.sleep(.45)
    with pytest.raises(AssignmentError) as refusal:
        await notes.service.get(caller=await caller(notes, method="GET"), note_id=intent.note_id)
    assert refusal.value.status_code == 404
    page = await notes.service.list(caller=await caller(notes, method="GET"))
    assert page.notes == () and rows(notes)[0]["ciphertext"] is not None


async def test_query_bounds_and_missing_human_are_closed(notes):
    selected = await caller(notes, method="GET")
    for kwargs in ({"limit": True}, {"limit": 101}, {"after_id": "bad"},
                   {"search": "x"*257}, {"search": "a\x00b"}, {"search": "\ud800"}):
        with pytest.raises(AssignmentError) as error:
            await notes.service.list(caller=selected, **kwargs)
        assert error.value.code == "explicit_note_query_invalid"
    with pytest.raises(AssignmentError):
        await notes.service.get(caller=selected, note_id="bad")
    for untrusted in (None, SimpleNamespace(owner_id=notes.api.fixture[1])):
        with pytest.raises(AssignmentError):
            await notes.service.list(caller=untrusted)
        with pytest.raises(AssignmentError):
            await notes.service.command(caller=untrusted, body=body())


async def test_expiry_during_required_audit_rolls_back_acceptance(notes, monkeypatch):
    from audit.repository import AuditRepository
    original = AuditRepository.insert_in_transaction

    def delayed(self, event, **kwargs):
        result = original(self, event, **kwargs)
        time.sleep(.4)
        return result

    selected = await caller(notes)
    intent = body(expires_at=time.time_ns()//1_000_000 + 300)
    monkeypatch.setattr(AuditRepository, "insert_in_transaction", delayed)
    with pytest.raises(AssignmentError):
        await notes.service.command(caller=selected, body=intent)
    assert not rows(notes) and not events(notes)


async def test_concurrent_bare_human_forget_replay_has_one_erasure_audit(notes):
    intent = body()
    await apply(notes, intent)
    command = ExplicitNoteCommand(command="forget", note_id=intent.note_id, expected_revision=1)
    callers = [await caller(notes, cookie=False) for _ in range(2)]
    results = await asyncio.gather(*(notes.service.command(caller=human, body=command) for human in callers))
    assert results[0] == results[1] and len(events(notes)) == 2
    assert rows(notes)[0]["ciphertext"] is None


async def test_expiry_batch_erases_current_rows_and_preserves_minimal_tombstones(notes):
    expiry = time.time_ns()//1_000_000 + 700
    for _ in range(3):
        await apply(notes, expires_at=expiry)
    await asyncio.sleep(max(0, (expiry-time.time_ns()//1_000_000)/1000) + .05)
    first = await notes.service.expire_batch(limit=2)
    assert (first.scanned, first.erased, first.skipped, first.cycle_complete) == (2, 2, 0, False)
    second = await notes.service.expire_batch(limit=2)
    assert (second.scanned, second.erased, second.cycle_complete) == (1, 1, True)
    third = await notes.service.expire_batch(limit=2)
    assert (third.scanned, third.erased, third.cycle_complete) == (0, 0, True)
    assert all(row["ciphertext"] is None and row["revision"] == 2 for row in rows(notes))
    expired_events = [event for event in events(notes) if event["action_type"] == "explicit_note_expire"]
    assert len(expired_events) == 3
    assert all(event["auth_principal"] == "system:explicit-note-expiry" for event in expired_events)
    assert notes.api.service.audit.verify_chain(notes.api.fixture[1]) is None


async def test_expiry_audit_failure_preserves_ciphertext_and_retry_can_erase(notes, monkeypatch):
    from audit.repository import AuditRepository
    expiry = time.time_ns()//1_000_000 + 250
    await apply(notes, expires_at=expiry)
    await asyncio.sleep(.3)
    original = AuditRepository.insert_in_transaction

    def refused(self, event, **kwargs):
        if event.action_type == "explicit_note_expire":
            raise RuntimeError("synthetic audit outage")
        return original(self, event, **kwargs)

    monkeypatch.setattr(AuditRepository, "insert_in_transaction", refused)
    with pytest.raises(AssignmentError):
        await notes.service.expire_batch()
    assert rows(notes)[0]["ciphertext"] is not None and len(events(notes)) == 1
    monkeypatch.setattr(AuditRepository, "insert_in_transaction", original)
    result = await notes.service.expire_batch()
    assert result.erased == 1 and rows(notes)[0]["ciphertext"] is None
    with pytest.raises(AssignmentError):
        await notes.service.expire_batch(limit=True)


async def test_application_owned_expiry_loop_erases_then_shutdown_joins_before_plane(notes, monkeypatch):
    from orchestrator.orchestrator import Orchestrator
    from tests.test_runtime_composition_074 import _StartAsyncTasks
    expiry = time.time_ns()//1_000_000 + 350
    await apply(notes, expires_at=expiry)
    await asyncio.sleep(.4)
    observed = asyncio.Event()
    original = ExplicitNoteService.expire_batch
    async def batch(self, *, limit):
        assert limit == 20
        result = await original(self, limit=limit)
        observed.set()
        return result
    monkeypatch.setattr(ExplicitNoteService, "expire_batch", batch)
    host = Orchestrator.__new__(Orchestrator)
    history = []
    host.async_task_manager = _StartAsyncTasks(history)
    host.human_request_boundary = notes.boundary
    worker = host._track_startup_background_task(notes.service.expiry_loop(), name="explicit-note-expiry")
    async def close_plane():
        assert worker.done() and notes.boundary.closed
        history.append("plane.closed")
    host.runtime_composition = SimpleNamespace(close=close_plane)
    try:
        await asyncio.wait_for(observed.wait(), 3)
        assert rows(notes)[0]["ciphertext"] is None
        await host._close_started_services()
        assert worker.cancelled() and history[-1] == "plane.closed"
        assert not host._startup_background_tasks
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_expiry_loop_failure_is_private_and_retains_existing_ciphertext(notes, monkeypatch, caplog):
    saved = await apply(notes)
    observed = asyncio.Event()
    async def failed(self, *, limit):
        observed.set()
        raise RuntimeError("PRIVATE_NOTE_CONTENT")
    monkeypatch.setattr(ExplicitNoteService, "expire_batch", failed)
    worker = asyncio.create_task(notes.service.expiry_loop())
    try:
        await asyncio.wait_for(observed.wait(), 2)
        assert not worker.done()
        assert "explicit_note_expiry_retry" in caplog.text and "PRIVATE_NOTE_CONTENT" not in caplog.text
        assert rows(notes)[0]["ciphertext"] is not None and rows(notes)[0]["revision"] == saved.revision
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_foreign_ciphertext_metadata_tamper_refuses_without_leaking_value(notes):
    first = await apply(notes)
    second = await apply(notes, value="Use structured tables")
    current = {str(row["note_id"]): row for row in rows(notes)}
    with notes.api.runtime.transaction() as tx:
        tx.execute("UPDATE explicit_note_current SET ciphertext=%s WHERE note_id=%s",
                   (bytes(current[first.note_id]["ciphertext"]), second.note_id))
    with pytest.raises(AssignmentError) as error:
        await notes.service.list(caller=await caller(notes, method="GET"))
    assert error.value.code == "explicit_note_unavailable"
    assert "structured tables" not in str(error.value) and len(events(notes)) == 2


async def test_busy_expiry_owner_does_not_block_later_owner_and_next_cycle_retries(notes):
    expiry = time.time_ns()//1_000_000 + 700
    first = await apply(notes, expires_at=expiry)
    second = await notes.service.command(caller=await caller(notes, cookie=False, owner="zzzz-later-owner"),
                                        body=body(expires_at=expiry))
    await asyncio.sleep(max(0, (expiry-time.time_ns()//1_000_000)/1000) + .05)
    with notes.api.runtime.transaction() as tx:
        tx.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended(%s,79))",
                     (notes.api.fixture[1],))
        batch = await notes.service.expire_batch(limit=2)
        assert (batch.scanned, batch.erased, batch.skipped, batch.cycle_complete) == (2, 1, 1, True)
        current = {str(row["note_id"]): row for row in rows(notes)}
        assert current[first.note_id]["ciphertext"] is not None
        assert current[second.note_id]["ciphertext"] is None
    next_cycle = await notes.service.expire_batch(limit=2)
    assert next_cycle.erased == 1 and rows(notes)[0]["ciphertext"] is None


async def test_database_expiry_during_decryption_refuses_even_when_host_clock_lags(notes, monkeypatch):
    from personalization import explicit_note_service as module
    expiry = time.time_ns()//1_000_000 + 450
    saved = await apply(notes, expires_at=expiry)
    # Only this diagnostic host observation lags; the real PG clock advances.
    monkeypatch.setattr(module, "_now", lambda: saved.updated_at)
    original = ExplicitNoteService._open

    def delayed(self, record, owner_id):
        result = original(self, record, owner_id)
        time.sleep(max(0, (expiry-time.time_ns()//1_000_000)/1000) + .08)
        return result

    monkeypatch.setattr(ExplicitNoteService, "_open", delayed)
    with pytest.raises(AssignmentError) as error:
        await notes.service.get(caller=await caller(notes, method="GET"), note_id=saved.note_id)
    assert error.value.code == "explicit_note_changed"


async def test_cancelled_privacy_requests_keep_worker_capacity_until_actual_completion(notes, monkeypatch):
    release = threading.Event()
    both_started = threading.Event()
    guard = threading.Lock()
    started = finished = 0

    def scan(_):
        nonlocal started, finished
        with guard:
            started += 1
            if started >= 2:
                both_started.set()
        try:
            assert release.wait(3)
            return False
        finally:
            with guard:
                finished += 1

    monkeypatch.setattr(phi_gate, "_GATE", SimpleNamespace(contains_phi=scan))
    callers = [await caller(notes) for _ in range(3)]
    tasks = [asyncio.create_task(notes.service.command(caller=human, body=body())) for human in callers[:2]]
    third = None
    try:
        assert await asyncio.to_thread(both_started.wait, 2)
        short = replace(callers[2], _deadline=time.monotonic()+.15)
        third = asyncio.create_task(notes.service.command(caller=short, body=body()))
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(.04)
        with guard:
            assert started == 2, "cancelled requests must not free still-running privacy capacity"
        with pytest.raises(AssignmentError) as timeout:
            await asyncio.wait_for(asyncio.shield(third), .5)
        assert timeout.value.status_code == 408
    finally:
        release.set()
        if third is not None:
            await asyncio.gather(third, return_exceptions=True)
        until = time.monotonic()+2
        while finished != started and time.monotonic() < until:
            await asyncio.sleep(.01)
        assert finished == started
    assert not rows(notes) and not events(notes)
