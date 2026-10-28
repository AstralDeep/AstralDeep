"""Push eligibility follows exact server session custody across lost responses,
logout, refresh and same-owner replacement using the owning public Plane facade.
"""

from datetime import timedelta

import pytest

from orchestrator.push_destinations import PushDestinationStore, DestinationConflict, DestinationStoreUnavailable
from orchestrator.push_delivery import AckRejected
from tests.helpers.push_sessions import consent_session, current_consent
from tests import test_push_delivery_270 as delivery_fixtures

plane_runtime = delivery_fixtures.plane_runtime
owner = delivery_fixtures.owner
encryption_key = delivery_fixtures.encryption_key
audit_repository = delivery_fixtures.audit_repository
clean_outbox = delivery_fixtures.clean_outbox
coordinator = delivery_fixtures.coordinator
store = delivery_fixtures.store
FakeAdapter = delivery_fixtures.FakeAdapter
_notification = delivery_fixtures._notification


def test_conditional_enrollment_requires_current_owner_consent(store, owner):
    with pytest.raises(DestinationStoreUnavailable):
        PushDestinationStore.enroll(store, owner, device_id="device-session01", platform="android", token="token",
                                    expected_destination_revision=0)
    _, wrong = consent_session(store.sessions, "other-owner")
    with pytest.raises(DestinationStoreUnavailable):
        store.enroll(owner, device_id="device-session01", platform="android", token="token",
                     expected_destination_revision=0, consent_session=wrong)


async def test_committed_lost_response_then_logout_denies_target_dispatch_and_ack(store, owner, coordinator, monkeypatch):
    sid, selected = consent_session(store.sessions, owner)
    store.enroll(owner, device_id="device-session01", platform="android", token="token",
                 expected_destination_revision=0, consent_session=selected)
    adapter = FakeAdapter()
    push = coordinator({"fcm": adapter})
    assert await push.enqueue_notification(owner, _notification(notification_id="lost-response")) == 1
    from orchestrator import web_auth
    monkeypatch.setattr(web_auth, "_get_store", lambda: store.sessions)
    observed = web_auth._session_from_row(store.sessions.get(sid))
    assert await web_auth._kill_session(sid, observed, request_execution=True)
    assert store.delivery_target(owner, "device-session01") is None
    assert store.delivery_targets(owner) == []
    await push.dispatch_once()
    assert adapter.calls == []
    with pytest.raises(AckRejected):
        await push.acknowledge(owner, "device-session01", "lost-response")


def test_refresh_preserves_custody_but_new_session_requires_revision_claim(store, owner):
    sid, selected = consent_session(store.sessions, owner)
    first = store.enroll(owner, device_id="device-session01", platform="android", token="token",
                         expected_destination_revision=0, consent_session=selected)
    store.sessions.update_tokens(sid, access_token="refreshed", refresh_token="refresh2",
                                 expected_incarnation_id=selected.observation.credential.incarnation_id)
    assert store.delivery_target(owner, first["device_id"]) is not None
    other_sid, other = consent_session(store.sessions, owner)
    legacy = store.enroll(owner, device_id=first["device_id"], platform="android", token="token",
                          consent_session=other)
    assert legacy == first
    with pytest.raises(DestinationConflict):
        store.enroll(owner, device_id=first["device_id"], platform="android", token="token",
                     expected_destination_revision=0, consent_session=other)
    second = store.enroll(owner, device_id=first["device_id"], platform="android", token="token",
                          expected_destination_revision=first["updated_at"], consent_session=other)
    store.sessions.delete(sid, expected_incarnation_id=selected.observation.credential.incarnation_id)
    assert store.delivery_target(owner, first["device_id"])["destination_revision"] == second["updated_at"]
    store.sessions.delete(other_sid, expected_incarnation_id=other.observation.credential.incarnation_id)
    assert store.delivery_target(owner, first["device_id"]) is None


def test_session_replay_hard_expiry_and_unbound_legacy_are_ineligible(store, owner, monkeypatch):
    sid, selected = consent_session(store.sessions, owner)
    first = store.enroll(owner, device_id="device-session01", platform="android", token="token",
                         expected_destination_revision=0, consent_session=selected)
    store.sessions.delete(sid, expected_incarnation_id=selected.observation.credential.incarnation_id)
    consent_session(store.sessions, owner, sid)
    assert store.delivery_target(owner, first["device_id"]) is None
    with pytest.raises(DestinationStoreUnavailable):
        store.enroll(owner, device_id=first["device_id"], platform="android", token="token",
                     expected_destination_revision=first["updated_at"], consent_session=selected)
    PushDestinationStore.enroll(store, owner, device_id="device-legacy001", platform="android", token="legacy")
    assert store.delivery_target(owner, "device-legacy001") is None
    expired = current_consent(store.sessions, owner, sid)
    expired = type(expired)(type(expired.observation)(expired.observation.credential,
        expired.observation.started_at - timedelta(minutes=1), expired.observation.valid_until - timedelta(minutes=1)))
    with pytest.raises(DestinationStoreUnavailable):
        store.enroll(owner, device_id="device-expired01", platform="android", token="token",
                     expected_destination_revision=0, consent_session=expired)


async def test_session_store_outage_retries_without_provider_invocation(store, owner, coordinator, monkeypatch):
    store.enroll(owner, device_id="device-session01", platform="android", token="token")
    adapter = FakeAdapter()
    push = coordinator({"fcm": adapter})
    await push.enqueue_notification(owner, _notification(notification_id="custody-outage"))
    sessions = store._credentials.plane_runtime.repositories.history.sessions
    def unavailable(*args, **kwargs):
        raise RuntimeError("synthetic database outage")
    monkeypatch.setattr(sessions, "get_execution_state", unavailable)
    with pytest.raises(DestinationStoreUnavailable):
        store.delivery_target(owner, "device-session01")
    await push.dispatch_once()
    assert adapter.calls == []
    row = delivery_fixtures._outbox_row(store._credentials.plane_runtime, owner)
    assert row["status"] == "retry" and row["last_error_code"] == "destination_unavailable"


async def test_session_retirement_during_policy_read_denies_provider_readmission(store, owner, coordinator):
    sid, selected = consent_session(store.sessions, owner)
    store.enroll(owner, device_id="device-session01", platform="android", token="token",
                 expected_destination_revision=0, consent_session=selected)
    adapter = FakeAdapter()
    def policy(*args):
        store.sessions.delete(sid, expected_incarnation_id=selected.observation.credential.incarnation_id)
        return True
    push = coordinator({"fcm": adapter}, delivery_policy=policy)
    await push.enqueue_notification(owner, _notification(notification_id="retired-during-policy"))
    await push.dispatch_once()
    assert adapter.calls == []


def test_concurrent_refresh_rechecks_stable_incarnation_without_losing_eligibility(store, owner, monkeypatch):
    first = store.enroll(owner, device_id="device-session01", platform="android", token="token")
    sessions = store._credentials.plane_runtime.repositories.history.sessions
    original = sessions.assert_current_consent
    refreshed = []
    def refresh_then_assert(transaction, *, observation):
        if not refreshed:
            refreshed.append(True)
            store.sessions.update_tokens(store.session_ids[owner], access_token="new-access", refresh_token="new-refresh",
                expected_incarnation_id=observation.credential.incarnation_id)
        return original(transaction, observation=observation)
    monkeypatch.setattr(sessions, "assert_current_consent", refresh_then_assert)
    assert store.delivery_target(owner, first["device_id"]) is not None


def test_hard_expired_session_with_matching_private_anchors_cannot_authorize_target(store, owner):
    from dataclasses import replace
    import time
    import uuid
    runtime = store._credentials.plane_runtime
    first = store.enroll(owner, device_id="device-session01", platform="android", token="token")
    sessions = runtime.repositories.history.sessions
    with runtime.transaction() as transaction:
        live = sessions.get(transaction, owner_id=owner, session_id=store.session_ids[owner])
        past = int(time.time()) - 3600
        expired = sessions.put(transaction, record=replace(live, session_id=uuid.uuid4().hex,
            incarnation_id=None, created_at=past, interactive_anchor=past,
            hard_expires_at=past + 30, last_refresh_at=past))
        record = store._get_record(transaction, owner, first["device_id"])
        record["session_reference"] = {key: getattr(expired, key) for key in
            ("session_id", "incarnation_id", "created_at", "interactive_anchor")}
        record["updated_at"] += 1
        store._write_record(transaction, owner, record, expected_updated_at=first["updated_at"])
    assert store.delivery_target(owner, first["device_id"]) is None
