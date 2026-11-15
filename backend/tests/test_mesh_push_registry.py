"""Exercises owner-isolated mesh wake registrations and opaque capability use over real Plane transactions.
The publisher alone retains APNs tokens; rotations, revocations, expiry and shared rate counters are checked without network services."""

from __future__ import annotations

import dataclasses
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from audit.repository import AuditRepository
from orchestrator.mesh_push_registry import MeshPushError, MeshPushRegistry, NAMESPACE, registry_key
from tests.helpers.voice_plane_runtime import isolated_plane_runtime

KEY = b"uR3Wb5rK8pHq2nTxE1sYd0cV7mLwZa4Gf9OiUjBhNkQ="
MESH = "sha256:" + "ab" * 32
DEVICE = "sha256:" + "cd" * 32
OTHER = "sha256:" + "ef" * 32


class Clock:
    def __init__(self):
        self.now = 1_800_000_000_000

    def __call__(self):
        return self.now


@pytest.fixture(scope="module")
def plane():
    with isolated_plane_runtime("mesh_push_registry") as runtime:
        yield runtime


@pytest.fixture
def registry(plane):
    clock = Clock()
    audit = AuditRepository(plane_runtime=plane, plane_repositories=plane.repositories)
    return MeshPushRegistry(plane_runtime=plane, audit_repository=audit, encryption_key=lambda: KEY, clock=clock), clock


@pytest.fixture
def owner():
    return "oidc.0123456789abcdef." + uuid4().hex


def enroll(registry, owner, *, mesh=MESH, device=DEVICE, token="apns-private-token", revision=0):
    return registry.register(owner, mesh_id=mesh, device_id=device, token=token, expected_revision=revision)


def denied(code, call):
    with pytest.raises(MeshPushError) as error:
        call()
    assert error.value.code == code
    assert "apns-private-token" not in str(error.value)


def test_registration_is_owner_bound_encrypted_and_never_puts_apns_token_in_capability(registry, plane, owner):
    store, clock = registry
    created = enroll(store, owner)
    assert created.publisher == "astral" and created.mesh_id == MESH and created.device_id == DEVICE
    assert created.revision == 1 and clock.now < created.expires_at_ms <= clock.now + 86_400_000
    assert 32 <= len(created.capability) <= 2048 and "apns-private-token" not in created.capability
    assert "apns-private-token" not in repr(created) and created.capability not in repr(created)
    with plane.transaction() as transaction:
        row = plane.repositories.credentials.get_credential(transaction, owner_id=owner, agent_id=NAMESPACE,
                                                          credential_key=registry_key(MESH, DEVICE))
    assert "apns-private-token" not in row.encrypted_value
    decoded = json.loads(Fernet(KEY).decrypt(row.encrypted_value.encode()))
    assert decoded["token"] == "apns-private-token" and created.capability not in row.encrypted_value
    status = store.status(owner, mesh_id=MESH, device_id=DEVICE)
    assert dataclasses.asdict(status).keys() == {"mesh_id", "device_id", "revision", "state", "expires_at_ms"}
    denied("registration_missing", lambda: store.status("different-owner", mesh_id=MESH, device_id=DEVICE))
    target = store.admit(created.capability, mesh_id=MESH, device_id=DEVICE)
    assert target.owner_id == owner and target.token == "apns-private-token"
    assert "apns-private-token" not in repr(target)
    events = store.audit_repository.list_for_user(owner, limit=10)[0]
    assert events and all("apns-private-token" not in event.model_dump_json() for event in events)
    assert all(created.capability not in event.model_dump_json() for event in events)


def test_rotation_revokes_old_capability_and_cas_refuses_a_stale_writer(registry, owner):
    store, clock = registry
    first = enroll(store, owner)
    second = enroll(store, owner, token="rotated-token", revision=first.revision)
    assert second.revision == first.revision + 1 and second.capability != first.capability
    denied("revision_conflict", lambda: enroll(store, owner, revision=first.revision))
    denied("capability_invalid", lambda: store.admit(first.capability, mesh_id=MESH, device_id=DEVICE))
    assert store.admit(second.capability, mesh_id=MESH, device_id=DEVICE).token == "rotated-token"
    revoked = store.revoke(owner, mesh_id=MESH, device_id=DEVICE, expected_revision=second.revision)
    assert revoked.state == "revoked" and revoked.revision > second.revision
    denied("capability_invalid", lambda: store.admit(second.capability, mesh_id=MESH, device_id=DEVICE))
    denied("revision_conflict", lambda: enroll(store, owner))
    third = enroll(store, owner, revision=revoked.revision)
    denied("rate_limited", lambda: store.admit(third.capability, mesh_id=MESH, device_id=DEVICE))
    clock.now += 300_000
    assert store.admit(third.capability, mesh_id=MESH, device_id=DEVICE).token == "apns-private-token"


def test_capability_refuses_wrong_mesh_device_tampering_and_expiry(registry, owner):
    store, clock = registry
    created = enroll(store, owner)
    for mesh, device in [(OTHER, DEVICE), (MESH, OTHER)]:
        denied("capability_invalid", lambda: store.admit(created.capability, mesh_id=mesh, device_id=device))
    for capability in ["", "x" * 2049, created.capability[:-8] + "abcdefgh"]:
        denied("capability_invalid", lambda: store.admit(capability, mesh_id=MESH, device_id=DEVICE))
    clock.now = created.expires_at_ms
    denied("capability_expired", lambda: store.admit(created.capability, mesh_id=MESH, device_id=DEVICE))


def test_rate_budget_is_shared_durable_and_does_not_change_administrative_revision(registry, plane, owner):
    store, clock = registry
    created = enroll(store, owner)
    other = MeshPushRegistry(plane_runtime=plane, audit_repository=store.audit_repository,
                            encryption_key=lambda: KEY, clock=clock)
    for index in range(4):
        (store if index % 2 else other).admit(created.capability, mesh_id=MESH, device_id=DEVICE)
        if index < 3:
            denied("rate_limited", lambda: store.admit(created.capability, mesh_id=MESH, device_id=DEVICE))
        clock.now += 300_000
    assert store.status(owner, mesh_id=MESH, device_id=DEVICE).revision == created.revision
    denied("rate_limited", lambda: other.admit(created.capability, mesh_id=MESH, device_id=DEVICE))
    clock.now += 3_600_000
    assert other.admit(created.capability, mesh_id=MESH, device_id=DEVICE).token == "apns-private-token"
    assert store.revoke(owner, mesh_id=MESH, device_id=DEVICE, expected_revision=created.revision).state == "revoked"


def test_two_concurrent_wakes_share_one_admission(registry, owner):
    store, _ = registry
    created = enroll(store, owner)
    barrier = threading.Barrier(2)

    def wake(_):
        barrier.wait(timeout=10)
        try:
            store.admit(created.capability, mesh_id=MESH, device_id=DEVICE)
            return "accepted"
        except MeshPushError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(wake, range(2)))
    assert outcomes.count("accepted") == 1 and set(outcomes) <= {"accepted", "rate_limited", "revision_conflict"}


def test_a_late_invalid_token_response_cannot_revoke_a_rotation(registry, owner):
    store, clock = registry
    first = enroll(store, owner)
    target = store.admit(first.capability, mesh_id=MESH, device_id=DEVICE)
    next = enroll(store, owner, token="new-token", revision=first.revision)
    assert store.invalidate(target) is False
    clock.now += 300_000
    current = store.admit(next.capability, mesh_id=MESH, device_id=DEVICE)
    assert current.token == "new-token"
    assert store.invalidate(current) is True
    denied("capability_invalid", lambda: store.admit(next.capability, mesh_id=MESH, device_id=DEVICE))


@pytest.mark.parametrize("field,value", [("mesh_id", "mesh"), ("device_id", "device"), ("device_id", OTHER.upper()),
                                        ("token", ""), ("token", "x" * 4097), ("token", "bad\nvalue"),
                                        ("expected_revision", True), ("expected_revision", -1),
                                        ("expected_revision", 2**53), ("expected_revision", 1.0)])
def test_registration_inputs_fail_closed(registry, owner, field, value):
    store, _ = registry
    arguments = dict(mesh_id=MESH, device_id=DEVICE, token="apns-private-token", expected_revision=0)
    arguments[field] = value
    denied("registration_invalid", lambda: store.register(owner, **arguments))


def test_missing_crypto_or_audit_never_creates_a_destination(registry, plane, owner):
    store, clock = registry
    for key in [b"", b"not-a-fernet-key"]:
        unavailable = MeshPushRegistry(plane_runtime=plane, audit_repository=store.audit_repository,
                                       encryption_key=lambda: key, clock=clock)
        denied("publisher_unavailable", lambda: enroll(unavailable, owner))
    no_audit = MeshPushRegistry(plane_runtime=plane, audit_repository=None, encryption_key=lambda: KEY, clock=clock)
    denied("publisher_unavailable", lambda: enroll(no_audit, owner))
    denied("registration_missing", lambda: store.status(owner, mesh_id=MESH, device_id=DEVICE))


def rewrite_record(plane, owner, change):
    with plane.transaction() as transaction:
        row = plane.repositories.credentials.get_credential(transaction, owner_id=owner, agent_id=NAMESPACE,
                                                          credential_key=registry_key(MESH, DEVICE))
        record = json.loads(Fernet(KEY).decrypt(row.encrypted_value.encode()))
        change(record)
        plane.repositories.credentials.compare_and_set_ciphertext(transaction, owner_id=owner, agent_id=NAMESPACE,
            credential_key=registry_key(MESH, DEVICE), expected_updated_at=row.updated_at, updated_at=row.updated_at + 1,
            encrypted_value=Fernet(KEY).encrypt(json.dumps(record).encode()).decode())


@pytest.mark.parametrize("field,value", [("v", True), ("v", 2), ("owner_id", "different-owner"), ("mesh_id", OTHER),
                                        ("state", "unknown"), ("revision", True), ("expires_at_ms", 0),
                                        ("hits", [True]), ("hits", [2, 1]), ("hits", [1, 2, 3, 4, 5]),
                                        ("token", None), ("capability_digest", "bad")])
def test_corrupt_publisher_records_are_unavailable_not_deliverable(registry, plane, owner, field, value):
    store, _ = registry
    enroll(store, owner)
    rewrite_record(plane, owner, lambda record: record.update({field: value}))
    denied("publisher_unavailable", lambda: store.status(owner, mesh_id=MESH, device_id=DEVICE))


def test_non_ascii_or_whitespace_tokens_are_refused(registry, owner):
    store, _ = registry
    for token in ["é", " token "]:
        denied("registration_invalid", lambda: enroll(store, owner, token=token))


def test_final_target_guard_observes_expiry_revocation_and_rotation(registry, owner):
    store, clock = registry
    first = enroll(store, owner)
    target = store.admit(first.capability, mesh_id=MESH, device_id=DEVICE)
    assert store.current(target)
    next = enroll(store, owner, token="new-token", revision=first.revision)
    assert not store.current(target)
    clock.now += 300_000
    current = store.admit(next.capability, mesh_id=MESH, device_id=DEVICE)
    assert store.current(current)
    clock.now = next.expires_at_ms
    assert not store.current(current)
    assert store.invalidate(current)
    assert not store.current(current)


def test_capacity_and_missing_or_stale_admin_targets_fail_closed(registry, owner):
    store, _ = registry
    denied("registration_missing", lambda: store.revoke(owner, mesh_id=MESH, device_id=DEVICE, expected_revision=1))
    for index in range(32):
        enroll(store, owner, device="sha256:" + f"{index:064x}")
    denied("registration_capacity", lambda: enroll(store, owner))
    first = store.status(owner, mesh_id=MESH, device_id="sha256:" + "0" * 64)
    denied("revision_conflict", lambda: store.revoke(owner, mesh_id=MESH, device_id=first.device_id,
                                                     expected_revision=first.revision + 1))
    revoked = store.revoke(owner, mesh_id=MESH, device_id=first.device_id, expected_revision=first.revision)
    assert store.revoke(owner, mesh_id=MESH, device_id=first.device_id, expected_revision=revoked.revision) == revoked


def test_failed_audit_receipt_rolls_back_registration(registry, plane, owner):
    from types import SimpleNamespace

    store, clock = registry
    failed = MeshPushRegistry(plane_runtime=plane, audit_repository=SimpleNamespace(insert_in_transaction=lambda *_a, **_k: None),
                              encryption_key=lambda: KEY, clock=clock)
    denied("publisher_unavailable", lambda: enroll(failed, owner))
    denied("registration_missing", lambda: store.status(owner, mesh_id=MESH, device_id=DEVICE))


@pytest.mark.parametrize("now", [True, 0, 2**53])
def test_invalid_publisher_clock_fails_closed(registry, owner, now):
    store, clock = registry
    clock.now = now
    denied("publisher_unavailable", lambda: enroll(store, owner))


def test_rotation_and_reactivation_do_not_reset_the_destination_rate_budget(registry, owner):
    store, clock = registry
    first = enroll(store, owner)
    store.admit(first.capability, mesh_id=MESH, device_id=DEVICE)
    second = enroll(store, owner, token="rotated-token", revision=first.revision)
    denied("rate_limited", lambda: store.admit(second.capability, mesh_id=MESH, device_id=DEVICE))
    clock.now += 300_000
    store.admit(second.capability, mesh_id=MESH, device_id=DEVICE)
    revoked = store.revoke(owner, mesh_id=MESH, device_id=DEVICE, expected_revision=second.revision)
    third = enroll(store, owner, revision=revoked.revision)
    denied("rate_limited", lambda: store.admit(third.capability, mesh_id=MESH, device_id=DEVICE))


def test_mesh_registry_rows_never_enter_generic_agent_credential_exports(registry, plane, owner, monkeypatch):
    from orchestrator.credential_manager import CredentialManager

    store, _ = registry
    enroll(store, owner)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", KEY.decode())
    manager = CredentialManager(plane_runtime=plane)
    assert manager.get_agent_credentials(owner, NAMESPACE) == {}
    assert manager.get_agent_credentials_encrypted(owner, NAMESPACE) == {}
