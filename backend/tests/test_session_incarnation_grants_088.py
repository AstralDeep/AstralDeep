"""Real PostgreSQL grant binding and original-consent transaction boundaries."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import json
import time
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from orchestrator import offline_grant as og
from orchestrator.session_consent import ConsentSession
from scheduler.store import ScheduledJobStore, ScheduleActionError
from tests.helpers.session_consent_088 import consent_from_store
from tests.helpers.session_plane_runtime import (
    get_session_record, isolated_plane_runtime, replace_session_record, web_session_store,
)


@pytest.fixture(scope="module")
def runtime():
    with isolated_plane_runtime("incarnation_grants_088") as value:
        yield value


@pytest.fixture
def fixture(runtime, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", key)
    monkeypatch.setattr(og, "OFFLINE_GRANT_ENC_KEY", key)
    monkeypatch.setenv("KEYCLOAK_TOKEN_URL", "https://incarnation-consent.invalid/token")
    monkeypatch.setattr(og.aiohttp, "ClientSession", lambda **kw: pytest.fail("No OAuth on refusal"))
    sessions = web_session_store(runtime)
    owner, sid = str(uuid4()), str(uuid4())
    sessions.create(sid, user_id=owner, access_token="same-synthetic-access",
                    refresh_token="same-synthetic-refresh", hard_max_seconds=3600)
    grants = og.OfflineGrantStore(plane_runtime=runtime)
    yield sessions, grants, owner, sid
    grants.revoke_for_user(owner)
    sessions.delete_for_user(owner)


def raw_grant(runtime, owner, plaintext):
    grant_id = str(uuid4())
    with runtime.transaction() as tx:
        runtime.repositories.offline_grants.create_grant(
            tx, grant_id=grant_id, owner_id=owner, agent_id=None,
            encrypted_refresh_token=og._fernet().encrypt(plaintext),
            issued_at=og._now_ms(), expires_at=og._now_ms()+60000)
    return grant_id


def test_newer_owner_session_never_replaces_approving_session(fixture):
    sessions, grants, owner, sid = fixture
    selected = consent_from_store(sessions, owner, sid)
    other = sessions.create(str(uuid4()), user_id=owner, access_token="same-synthetic-access",
                            refresh_token="same-synthetic-refresh", hard_max_seconds=3600)
    grant_id = grants.capture(owner, selected)
    reference = grants._resolve_reference(grants._grant(owner, grant_id))
    assert reference == selected.reference(owner)
    assert reference["incarnation_id"] != other["incarnation_id"]
    assert reference["session_id"] == sid


def test_recreated_identical_row_cannot_capture_old_consent(fixture, runtime):
    sessions, grants, owner, sid = fixture
    selected = consent_from_store(sessions, owner, sid)
    old = get_session_record(runtime, sid)
    new = replace_session_record(runtime, old)
    assert replace(old, incarnation_id=new.incarnation_id) == new
    assert old.incarnation_id != new.incarnation_id
    with pytest.raises(og.OfflineGrantError, match="re-consent"):
        grants.capture(owner, selected)
    assert grants.latest_valid_for(owner) is None


def test_missing_current_refresh_credential_cannot_capture_consent(fixture):
    sessions, grants, owner, sid = fixture
    selected = consent_from_store(sessions, owner, sid)
    sessions.update_tokens(sid, access_token="still-live-access", refresh_token="",
                           expected_incarnation_id=selected.observation.credential.incarnation_id)
    with pytest.raises(og.OfflineGrantError, match="re-consent"):
        grants.capture(owner, selected)
    assert grants.latest_valid_for(owner) is None


def test_recreated_identical_row_cannot_refresh_existing_grant(fixture, runtime):
    sessions, grants, owner, sid = fixture
    selected = consent_from_store(sessions, owner, sid)
    grant_id = grants.capture(owner, selected)
    stored = grants._grant(owner, grant_id)
    old = get_session_record(runtime, sid)
    new = replace_session_record(runtime, old)
    assert replace(old, incarnation_id=new.incarnation_id) == new
    with pytest.raises(og.OfflineGrantError, match="unavailable"):
        asyncio.run(grants.mint_access_token(grant_id, user_id=owner))
    assert grants._grant(owner, grant_id) == stored
    assert get_session_record(runtime, sid) == new


@pytest.mark.parametrize("kind", ["raw-token", "v1", "unknown-version"])
def test_legacy_identity_is_retained_but_never_rebound(fixture, runtime, kind):
    sessions, grants, owner, sid = fixture
    reference = consent_from_store(sessions, owner, sid).reference(owner)
    if kind == "raw-token":
        plaintext = b"same-synthetic-refresh"
    elif kind == "v1":
        reference.pop("incarnation_id")
        plaintext = b"\x00astral-offline-session/v1\x00"+json.dumps(reference).encode()
    else:
        plaintext = b"\x00astral-offline-session/v3\x00"+json.dumps(reference).encode()
    gid = raw_grant(runtime, owner, plaintext)
    before = grants._grant(owner, gid)
    with pytest.raises(og.OfflineGrantError, match="re-consent"):
        asyncio.run(grants.mint_access_token(gid, user_id=owner))
    assert grants._grant(owner, gid) == before


@pytest.mark.parametrize("field,value", [("incarnation_id",None), ("incarnation_id","bad"),
    ("incarnation_id","AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
    ("incarnation_id","aaaaaaaa-aaaa-1aaa-8aaa-aaaaaaaaaaaa"),
    ("created_at",True), ("interactive_anchor",-1), ("session_id","")])
def test_malformed_v2_reference_refuses_before_oauth(fixture, runtime, field, value):
    sessions, grants, owner, sid = fixture
    reference = consent_from_store(sessions, owner, sid).reference(owner)
    reference[field] = value
    gid = raw_grant(runtime, owner, og._SESSION_REFERENCE_PREFIX.encode()+json.dumps(reference).encode())
    with pytest.raises(og.OfflineGrantError, match="malformed"):
        asyncio.run(grants.mint_access_token(gid, user_id=owner))


def test_duplicate_and_extra_reference_fields_refuse(fixture, runtime):
    sessions, grants, owner, sid = fixture
    reference = consent_from_store(sessions, owner, sid).reference(owner)
    body = json.dumps(reference)
    bodies = [body[:-1]+', "session_id": "duplicate"}', json.dumps({**reference,"token":"forbidden"})]
    for body in bodies:
        gid = raw_grant(runtime, owner, og._SESSION_REFERENCE_PREFIX.encode()+body.encode())
        with pytest.raises(og.OfflineGrantError, match="malformed"):
            asyncio.run(grants.mint_access_token(gid, user_id=owner))


def test_expiry_after_grant_insert_rolls_back_entire_capture(fixture, runtime, monkeypatch):
    sessions, grants, owner, sid = fixture
    selected = consent_from_store(sessions, owner, sid)
    selected = ConsentSession(replace(selected.observation,
        valid_until=selected.observation.started_at+timedelta(seconds=1)))
    repository = runtime.repositories.offline_grants
    original = repository.create_grant
    inserted = []

    def delayed(tx, **kwargs):
        record = original(tx, **kwargs)
        inserted.append(record.grant_id)
        time.sleep(1.05)
        return record

    monkeypatch.setattr(repository, "create_grant", delayed)
    with pytest.raises(og.OfflineGrantError, match="re-consent"):
        grants.capture(owner, selected)
    assert len(inserted) == 1
    assert grants._grant(owner, inserted[0]) is None
    assert grants.latest_valid_for(owner) is None


def test_grant_capture_contention_is_bounded_and_has_no_partial_row(fixture, runtime):
    sessions, grants, owner, sid = fixture
    selected = consent_from_store(sessions, owner, sid)
    repository = runtime.repositories.history.sessions
    with runtime.transaction() as tx:
        repository.assert_current_consent(tx, observation=selected.observation)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(grants.capture, owner, selected)
            with pytest.raises(og.OfflineGrantError, match="re-consent"):
                future.result(timeout=5)
    assert grants.latest_valid_for(owner) is None


@pytest.mark.parametrize("kind", ["foreign", "missing", "wire-dict", "expired", "unknown-type"])
def test_unqualified_capture_never_creates_authority(fixture, kind):
    sessions, grants, owner, sid = fixture
    selected = consent_from_store(sessions, owner, sid)
    if kind == "foreign":
        selected = ConsentSession(replace(selected.observation,
            credential=replace(selected.observation.credential, owner_id="foreign")))
    elif kind == "wire-dict":
        selected = selected.reference(owner)
    elif kind == "missing":
        selected = None
    elif kind == "expired":
        selected = ConsentSession(replace(selected.observation, valid_until=selected.observation.started_at))
    else:
        selected = ConsentSession(object())
    with pytest.raises(og.OfflineGrantError, match="re-consent"):
        grants.capture(owner, selected)
    assert grants.latest_valid_for(owner) is None


def schedule_arguments(owner, grants, prepared, current=lambda: True):
    return dict(name="Synthetic consent schedule", instruction="Inspect the fixture",
        schedule_kind="cron", schedule_expr="0 8 * * *", timezone="UTC",
        consented_scopes=["tools:read"], agent_id=None, target_chat_id=None,
        next_run_at=int(time.time()*1000)+60000, offline_grant_id=None,
        prepared_consent=prepared, offline_grants=grants, consent_current=current)


def test_preparation_has_no_grant_until_dependent_schedule_commits(fixture, runtime):
    sessions, grants, owner, sid = fixture
    prepared = grants.prepare_capture(owner, consent_from_store(sessions, owner, sid))
    assert grants._grant(owner, prepared.grant_id) is None
    store = ScheduledJobStore(plane_runtime=runtime)
    job = store.create_job(owner, **schedule_arguments(owner, grants, prepared))
    assert job["offline_grant_id"] == prepared.grant_id
    assert store.get_job(owner, job["id"]) == job
    assert grants._resolve_reference(grants._grant(owner, prepared.grant_id)) == prepared.selected_session.reference(owner)


@pytest.mark.parametrize("stage", ["before", "after-insert", "expired-after-insert", "write-failure"])
def test_schedule_and_grant_roll_back_together(fixture, runtime, monkeypatch, stage):
    sessions, grants, owner, sid = fixture
    selected = consent_from_store(sessions, owner, sid)
    if stage == "expired-after-insert":
        selected = ConsentSession(replace(selected.observation,
            valid_until=selected.observation.started_at+timedelta(seconds=1)))
    prepared = grants.prepare_capture(owner, selected)
    store = ScheduledJobStore(plane_runtime=runtime)
    original = store._plane.repository.create_job_definition
    current = [stage != "before"]
    attempted = []

    def write(tx, *, job):
        attempted.append(job.job_id)
        if stage == "write-failure":
            raise RuntimeError("synthetic write refusal")
        result = original(tx, job=job)
        if stage == "after-insert":
            current[0] = False
        elif stage == "expired-after-insert":
            time.sleep(1.05)
        return result

    monkeypatch.setattr(store._plane.repository, "create_job_definition", write)
    with pytest.raises((ScheduleActionError, og.OfflineGrantError, RuntimeError)):
        store.create_job(owner, **schedule_arguments(owner, grants, prepared, lambda: current[0]))
    assert bool(attempted) == (stage != "before")
    assert grants._grant(owner, prepared.grant_id) is None
    assert store.list_jobs(owner) == []


@pytest.mark.parametrize("kind", ["foreign-runtime", "runtime-argument", "ciphertext", "reference", "grant-id", "wire-dict"])
def test_prepared_consent_cannot_change_its_binding(fixture, runtime, kind):
    sessions, grants, owner, sid = fixture
    prepared = grants.prepare_capture(owner, consent_from_store(sessions, owner, sid))
    runtime_argument = runtime
    if kind == "foreign-runtime":
        candidate = replace(prepared, plane_runtime=object())
    elif kind == "runtime-argument":
        candidate, runtime_argument = prepared, object()
    elif kind == "ciphertext":
        candidate = replace(prepared, encrypted_reference=b"invalid")
    elif kind == "reference":
        reference = prepared.selected_session.reference(owner)
        reference["incarnation_id"] = str(uuid4())
        candidate = replace(prepared, encrypted_reference=og._fernet().encrypt(grants._reference_bytes(reference)))
    elif kind == "grant-id":
        candidate = replace(prepared, grant_id="not-an-issued-id")
    else:
        candidate = {"grant_id": prepared.grant_id}
    with runtime.transaction() as tx:
        with pytest.raises(og.OfflineGrantError, match="re-consent"):
            grants.capture_in_transaction(tx, candidate, plane_runtime=runtime_argument)
    assert grants._grant(owner, prepared.grant_id) is None


@pytest.mark.parametrize("changed", [False, True])
def test_schedule_without_grant_also_preserves_original_registration(fixture, runtime, monkeypatch, changed):
    _, grants, owner, _ = fixture
    store = ScheduledJobStore(plane_runtime=runtime)
    original = store._plane.repository.create_job_definition
    current = [True]

    def write(tx, *, job):
        result = original(tx, job=job)
        current[0] = not changed
        return result

    monkeypatch.setattr(store._plane.repository, "create_job_definition", write)
    arguments = schedule_arguments(owner, None, None, lambda: current[0])
    if changed:
        with pytest.raises(ScheduleActionError, match="consent_unavailable"):
            store.create_job(owner, **arguments)
        assert store.list_jobs(owner) == []
    else:
        job = store.create_job(owner, **arguments)
        assert job["offline_grant_id"] is None
        assert len(store.list_jobs(owner)) == 1
    assert grants.latest_valid_for(owner) is None


def test_schedule_write_lock_refuses_while_blocker_remains(fixture, runtime):
    sessions, grants, owner, sid = fixture
    prepared = grants.prepare_capture(owner, consent_from_store(sessions, owner, sid))
    store = ScheduledJobStore(plane_runtime=runtime)
    with runtime.transaction() as blocker:
        blocker.execute("LOCK TABLE scheduled_job IN ACCESS EXCLUSIVE MODE")
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(store.create_job, owner, **schedule_arguments(owner, grants, prepared))
            with pytest.raises(ScheduleActionError, match="schedule_write_unavailable"):
                pending.result(timeout=3)
            assert pending.done()
    assert grants._grant(owner, prepared.grant_id) is None
    assert store.list_jobs(owner) == []


def test_unknown_schedule_commit_retains_its_accepted_grant(fixture, runtime, monkeypatch):
    sessions, grants, owner, sid = fixture
    prepared = grants.prepare_capture(owner, consent_from_store(sessions, owner, sid))
    store = ScheduledJobStore(plane_runtime=runtime)
    original = store._plane.transaction

    @contextmanager
    def lost_ack():
        with original() as transaction:
            yield transaction
        raise RuntimeError("synthetic acknowledgement loss after commit")

    with monkeypatch.context() as patch:
        patch.setattr(store._plane, "transaction", lost_ack)
        with pytest.raises(ScheduleActionError, match="schedule_write_unavailable"):
            store.create_job(owner, **schedule_arguments(owner, grants, prepared))
    jobs = store.list_jobs(owner)
    assert len(jobs) == 1
    assert jobs[0]["offline_grant_id"] == prepared.grant_id
    assert grants._grant(owner, prepared.grant_id).active


def test_foreign_schedule_owner_cannot_use_prepared_consent(fixture, runtime):
    sessions, grants, owner, sid = fixture
    prepared = grants.prepare_capture(owner, consent_from_store(sessions, owner, sid))
    store = ScheduledJobStore(plane_runtime=runtime)
    with pytest.raises(ScheduleActionError, match="consent_unavailable"):
        store.create_job("other-owner", **schedule_arguments(owner, grants, prepared))
    assert grants._grant(owner, prepared.grant_id) is None
    assert store.list_jobs("other-owner") == []
