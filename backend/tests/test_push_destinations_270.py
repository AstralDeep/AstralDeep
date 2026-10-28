"""Owner-bound push destination enrollment over real PostgreSQL: enrollment,
rotation, revocation, reactivation, capacity, input validation, owner isolation,
encrypted-at-rest tokens, and audited transitions including denials.
"""

from __future__ import annotations

import uuid
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.fernet import Fernet

from audit.repository import AuditRepository
from orchestrator import push_destinations as pd
from tests.helpers.voice_plane_runtime import isolated_plane_runtime
from tests.helpers.push_sessions import SessionBoundPushStore

KEY = "uR3Wb5rK8pHq2nTxE1sYd0cV7mLwZa4Gf9OiUjBhNkQ="


@pytest.fixture()
def owner() -> str:
    return f"push-owner-{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def other_owner() -> str:
    return f"push-owner-{uuid.uuid4().hex[:12]}"


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("push_destinations") as runtime:
        yield runtime


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setenv(pd._CREDENTIAL_KEY_SOURCE, KEY)
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", KEY)


@pytest.fixture()
def audit_repository(plane_runtime):
    return AuditRepository(
        plane_runtime=plane_runtime, plane_repositories=runtime_repositories(plane_runtime)
    )


def runtime_repositories(runtime):
    return runtime.repositories


@pytest.fixture()
def audit_events(monkeypatch, audit_repository) -> list:
    events = []
    original = audit_repository.insert_in_transaction

    def append(event, **kwargs):
        receipt = original(event, **kwargs)
        events.append(event)
        return receipt

    monkeypatch.setattr(audit_repository, "insert_in_transaction", append)
    return events


@pytest.fixture()
def store(plane_runtime, audit_repository) -> pd.PushDestinationStore:
    return SessionBoundPushStore(
        plane_runtime=plane_runtime,
        plane_repositories=runtime_repositories(plane_runtime),
        audit_repository=audit_repository,
    )


def _enroll(store, owner, device="device-aaaaaaaa", platform="android", token="fcm-token-1", label=None):
    return store.enroll(
        owner,
        device_id=device,
        platform=platform,
        token=token,
        label=label,
    )


def test_conditional_same_token_claim_advances_revision_not_generation(store, owner, audit_events, monkeypatch):
    first = store.enroll(owner, device_id="device-revision1", platform="android", token="token",
                         expected_destination_revision=0)
    monkeypatch.setattr(pd, "_now_ms", lambda: first["updated_at"] - 1000)
    second = store.enroll(owner, device_id="device-revision1", platform="android", token="token",
                          expected_destination_revision=first["updated_at"])
    assert second["updated_at"] > first["updated_at"]
    assert second["token_generation"] == first["token_generation"]
    assert audit_events[-1].action_type == "push.destination.claim"
    for stale in (0, first["updated_at"]):
        with pytest.raises(pd.DestinationConflict):
            store.enroll(owner, device_id="device-revision1", platform="android", token="token",
                         expected_destination_revision=stale)
    assert store.get_destination(owner, "device-revision1") == second


@pytest.mark.parametrize("revision", [True, False, -1, 2**53, 1.0, "1"])
def test_conditional_revision_requires_safe_strict_integer(store, owner, revision):
    with pytest.raises(pd.DestinationInvalid):
        store.enroll(owner, device_id="device-revision1", platform="android", token="token",
                     expected_destination_revision=revision)


def test_revision_fence_applies_to_revoked_rows_and_lifecycle_delete(store, owner):
    first = _enroll(store, owner)
    revoked = store.revoke(owner, first["device_id"])
    with pytest.raises(pd.DestinationConflict):
        store.enroll(owner, device_id=first["device_id"], platform="android", token="token",
                     expected_destination_revision=0)
    current = store.enroll(owner, device_id=first["device_id"], platform="android", token="token",
                           expected_destination_revision=revoked["updated_at"])
    with pytest.raises(pd.DestinationConflict):
        store.revoke(owner, first["device_id"], expected_token_generation=current["token_generation"],
                     expected_destination_revision=revoked["updated_at"])
    result = store.revoke(owner, first["device_id"], expected_token_generation=current["token_generation"],
                          expected_destination_revision=current["updated_at"])
    assert result["status"] == "revoked" and result["updated_at"] > current["updated_at"]


def test_concurrent_conditional_claims_have_one_winner(store, owner, monkeypatch):
    first = _enroll(store, owner)
    original_get = store._get_record
    barrier = threading.Barrier(2)
    observed = threading.local()
    def captured(transaction, owner_id, device_id):
        record = original_get(transaction, owner_id, device_id)
        if threading.current_thread().name.startswith("push-claim") and not getattr(observed, "ready", False):
            observed.ready = True
            barrier.wait(timeout=10)
        return record
    monkeypatch.setattr(store, "_get_record", captured)
    def claim(_):
        try:
            store.enroll(owner, device_id=first["device_id"], platform="android", token="fcm-token-1",
                         expected_destination_revision=first["updated_at"])
        except pd.DestinationConflict:
            return False
        return True
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="push-claim") as executor:
        assert list(executor.map(claim, range(2))).count(True) == 1
    assert store.get_destination(owner, first["device_id"])["updated_at"] > first["updated_at"]


def test_enroll_creates_active_destination(store, owner, audit_events):
    record = _enroll(store, owner, label="Pixel")
    assert record["status"] == "active"
    assert record["provider"] == "fcm"
    assert record["token_generation"] == 1
    assert record["label"] == "Pixel"
    assert "token" not in record
    assert "push_token" not in record
    assert [event.action_type for event in audit_events] == [
        "push.destination.enroll"
    ]


def test_enroll_is_idempotent_for_same_token(store, owner, audit_events):
    _enroll(store, owner)
    again = _enroll(store, owner)
    assert again["token_generation"] == 1
    assert len(audit_events) == 1


def test_rotation_replaces_token_and_bumps_generation(store, owner, audit_events):
    _enroll(store, owner)
    rotated = _enroll(store, owner, token="fcm-token-2")
    assert rotated["token_generation"] == 2
    assert rotated["rotated_at"] is not None
    target = store.delivery_target(owner, "device-aaaaaaaa")
    assert target["token_fingerprint"] == pd.token_fingerprint("fcm-token-2")
    assert [event.action_type for event in audit_events] == [
        "push.destination.enroll",
        "push.destination.rotate",
    ]


def test_revocation_and_reactivation(store, owner, audit_events):
    _enroll(store, owner)
    revoked = store.revoke(owner, "device-aaaaaaaa")
    assert revoked["status"] == "revoked"
    assert revoked["revoked_at"] is not None
    assert store.delivery_target(owner, "device-aaaaaaaa") is None
    assert store.delivery_targets(owner) == []
    again = _enroll(store, owner, token="fcm-token-3")
    assert again["status"] == "active"
    assert again["token_generation"] == 2
    assert [event.action_type for event in audit_events] == [
        "push.destination.enroll",
        "push.destination.revoke",
        "push.destination.reactivate",
    ]


def test_revoke_is_idempotent(store, owner):
    _enroll(store, owner)
    first = store.revoke(owner, "device-aaaaaaaa")
    second = store.revoke(owner, "device-aaaaaaaa")
    assert first["status"] == second["status"] == "revoked"


def test_revoke_unknown_destination_denied(store, owner):
    with pytest.raises(pd.DestinationNotFound):
        store.revoke(owner, "device-zzzzzzzz")


def test_wrong_owner_cannot_read_or_revoke(store, owner, other_owner):
    _enroll(store, owner)
    with pytest.raises(pd.DestinationNotFound):
        store.get_destination(other_owner, "device-aaaaaaaa")
    with pytest.raises(pd.DestinationNotFound):
        store.revoke(other_owner, "device-aaaaaaaa")
    assert store.list_destinations(other_owner) == []


def test_list_returns_only_own_destinations_sorted(store, owner):
    _enroll(store, owner, device="device-bbbbbbbb")
    _enroll(store, owner, device="device-aaaaaaaa", platform="ios", token="apns-token-1")
    listed = store.list_destinations(owner)
    assert [item["device_id"] for item in listed] == [
        "device-bbbbbbbb",
        "device-aaaaaaaa",
    ]
    assert listed[1]["provider"] == "apns"
    for item in listed:
        assert "push_token" not in item and "token" not in item


def test_token_is_encrypted_at_rest(store, owner, plane_runtime):
    _enroll(store, owner, token="very-secret-fcm-token")
    row = plane_runtime.fetch_one(
        "SELECT encrypted_value FROM user_credentials "
        "WHERE user_id = %s AND agent_id = %s",
        (owner, pd.DESTINATION_NAMESPACE),
    )
    assert row is not None
    ciphertext = row["encrypted_value"]
    assert "very-secret-fcm-token" not in ciphertext
    fernet = Fernet(KEY.encode())
    decrypted = fernet.decrypt(ciphertext.encode()).decode()
    assert "very-secret-fcm-token" in decrypted


def test_capacity_denies_active_destinations_beyond_bound(store, owner, audit_events):
    for index in range(pd.MAX_DESTINATIONS_PER_OWNER):
        _enroll(store, owner, device=f"device-cap-{index:04d}", token=f"token-{index}")
    with pytest.raises(pd.DestinationCapacity):
        _enroll(store, owner, device="device-cap-extra", token="token-extra")
    denial = audit_events[-1]
    assert denial.action_type == "push.destination.enroll_denied"
    assert denial.outcome == "failure"
    assert len(store.list_destinations(owner)) == pd.MAX_DESTINATIONS_PER_OWNER


def test_revoked_slot_frees_capacity(store, owner):
    for index in range(pd.MAX_DESTINATIONS_PER_OWNER):
        _enroll(store, owner, device=f"device-free-{index:04d}", token=f"token-{index}")
    store.revoke(owner, "device-free-0000")
    record = _enroll(store, owner, device="device-free-new", token="token-new")
    assert record["status"] == "active"


def test_reactivation_cannot_exceed_active_destination_capacity(store, owner):
    _enroll(store, owner, device="device-reactivate")
    store.revoke(owner, "device-reactivate")
    for index in range(pd.MAX_DESTINATIONS_PER_OWNER):
        _enroll(store, owner, device=f"device-active-{index:04d}")
    with pytest.raises(pd.DestinationCapacity):
        _enroll(store, owner, device="device-reactivate")
    assert store.get_destination(owner, "device-reactivate")["status"] == "revoked"


def test_concurrent_enrollment_cannot_exceed_active_destination_capacity(store, owner, monkeypatch):
    for index in range(pd.MAX_DESTINATIONS_PER_OWNER - 1):
        _enroll(store, owner, device=f"device-inventory-{index:04d}")
    original_get = store._get_record
    barrier = threading.Barrier(2)
    observed = threading.local()

    def same_inventory(transaction, owner_id, device_id):
        record = original_get(transaction, owner_id, device_id)
        if threading.current_thread().name.startswith("push-enroll") and not getattr(observed, "ready", False):
            observed.ready = True
            barrier.wait(timeout=10)
        return record

    def enroll_one(index):
        try:
            _enroll(store, owner, device=f"device-contender-{index:04d}")
        except pd.PushDestinationError:
            return False
        return True

    monkeypatch.setattr(store, "_get_record", same_inventory)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="push-enroll") as executor:
        results = list(executor.map(enroll_one, range(2)))
    assert results.count(True) == 1
    assert sum(item["status"] == "active" for item in store.list_destinations(owner)) == pd.MAX_DESTINATIONS_PER_OWNER


@pytest.mark.parametrize(
    "kwargs",
    [
        {"device": "short"},
        {"device": "-leading-dash-ok"},
        {"platform": "windows"},
        {"token": ""},
        {"token": "  "},
        {"token": "x" * (pd.MAX_TOKEN_CHARS + 1)},
        {"token": "bad\x00token"},
        {"label": "y" * (pd.MAX_LABEL_CHARS + 1)},
    ],
)
def test_invalid_enrollment_inputs_rejected(store, owner, kwargs):
    params = {
        "owner": owner,
        "device": "device-valid-01",
        "platform": "android",
        "token": "fcm-token-ok",
    }
    params.update(kwargs)
    with pytest.raises(pd.DestinationInvalid):
        _enroll(store, **params)


def test_delivery_targets_expose_raw_token_only_internally(store, owner):
    _enroll(store, owner, device="device-target-01", token="target-token-1")
    _enroll(store, owner, device="device-target-02", platform="ios", token="target-token-2")
    store.revoke(owner, "device-target-02")
    targets = store.delivery_targets(owner)
    assert len(targets) == 1
    assert targets[0] == {
        "device_id": "device-target-01",
        "provider": "fcm",
        "push_token": "target-token-1",
        "token_fingerprint": pd.token_fingerprint("target-token-1"),
        "token_generation": 1,
        "destination_revision": store.get_destination(owner, "device-target-01")["updated_at"],
    }


def test_store_without_encryption_key_fails_closed(plane_runtime, owner, monkeypatch, audit_repository):
    monkeypatch.delenv(pd._CREDENTIAL_KEY_SOURCE, raising=False)
    store = pd.PushDestinationStore(
        plane_runtime=plane_runtime,
        plane_repositories=runtime_repositories(plane_runtime),
        audit_repository=audit_repository,
    )
    with pytest.raises(pd.DestinationStoreUnavailable):
        _enroll(store, owner)


def test_audit_unavailable_blocks_enrollment(plane_runtime, owner, monkeypatch):
    store = pd.PushDestinationStore(
        plane_runtime=plane_runtime,
        plane_repositories=runtime_repositories(plane_runtime),
        audit_repository=None,
    )
    with pytest.raises(pd.DestinationStoreUnavailable):
        _enroll(store, owner)


def test_invalid_encryption_key_fails_closed(plane_runtime, owner, monkeypatch, audit_repository):
    monkeypatch.setenv(pd._CREDENTIAL_KEY_SOURCE, "not-a-valid-fernet-key")
    store = pd.PushDestinationStore(
        plane_runtime=plane_runtime,
        plane_repositories=runtime_repositories(plane_runtime),
        audit_repository=audit_repository,
    )
    with pytest.raises(pd.DestinationStoreUnavailable):
        _enroll(store, owner)


def test_undecryptable_record_fails_closed(plane_runtime, owner, store):
    _enroll(store, owner, token="tok-1")
    plane_runtime.execute(
        "UPDATE user_credentials SET encrypted_value = %s "
        "WHERE user_id = %s AND agent_id = %s",
        ("gAAAAA-invalid-ciphertext", owner, pd.DESTINATION_NAMESPACE),
    )
    with pytest.raises(pd.DestinationStoreUnavailable):
        store.list_destinations(owner)
    with pytest.raises(pd.DestinationStoreUnavailable):
        store.get_destination(owner, "device-aaaaaaaa")


def test_non_string_token_and_label_rejected(plane_runtime, owner, store):
    with pytest.raises(pd.DestinationInvalid):
        _enroll(store, owner, token=12345)
    with pytest.raises(pd.DestinationInvalid):
        _enroll(store, owner, label=12345)
    blank = _enroll(store, owner, label="   ")
    assert blank["label"] is None


def test_tampered_revision_fails_closed(plane_runtime, owner, store):
    _enroll(store, owner, token="tok-1")
    plane_runtime.execute(
        "UPDATE user_credentials SET updated_at = updated_at + 1 "
        "WHERE user_id = %s AND agent_id = %s",
        (owner, pd.DESTINATION_NAMESPACE),
    )
    with pytest.raises(pd.DestinationStoreUnavailable):
        store.get_destination(owner, "device-aaaaaaaa")


def test_destination_audit_unavailable_on_transition(
    plane_runtime, owner, audit_repository, monkeypatch
):
    store = pd.PushDestinationStore(
        plane_runtime=plane_runtime,
        plane_repositories=runtime_repositories(plane_runtime),
        audit_repository=audit_repository,
    )
    _enroll(store, owner, token="tok-1")

    def broken(event, **kwargs):
        raise RuntimeError("audit chain unavailable")

    monkeypatch.setattr(audit_repository, "insert_in_transaction", broken)
    with pytest.raises(pd.DestinationStoreUnavailable):
        store.revoke(owner, "device-aaaaaaaa")
    assert store.get_destination(owner, "device-aaaaaaaa")["status"] == "active"


def test_stale_rotation_cannot_overwrite_concurrent_revocation(store, owner, monkeypatch):
    _enroll(store, owner)
    original_get = store._get_record
    raced = False

    def revoke_after_read(transaction, owner_id, device_id):
        nonlocal raced
        record = original_get(transaction, owner_id, device_id)
        if not raced:
            raced = True
            store.revoke(owner_id, device_id)
        return record

    monkeypatch.setattr(store, "_get_record", revoke_after_read)
    with pytest.raises(pd.PushDestinationError):
        _enroll(store, owner, token="new-token")
    assert store.get_destination(owner, "device-aaaaaaaa")["status"] == "revoked"
