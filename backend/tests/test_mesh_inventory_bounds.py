"""Verifies bounded Plane credential reads never authorize a truncated mesh inventory.
MeshEnrollmentStore must refuse saturated owner-scoped reads before deciding whether
a public device identity is available for enrollment or member admission.
"""

from types import SimpleNamespace

import pytest

from orchestrator import mesh_enrollment as me


class BoundedCredentials:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def list_credentials(self, transaction, *, owner_id, agent_id, limit):
        self.calls.append((owner_id, agent_id, limit))
        return tuple(
            SimpleNamespace(encrypted_value=record)
            for record in self.records
            if record["owner_id"] == owner_id and record["namespace"] == agent_id
        )[:limit]


def store_for(monkeypatch, records):
    repository = BoundedCredentials(records)
    store = object.__new__(me.MeshEnrollmentStore)
    store._credentials = SimpleNamespace(
        repository=repository,
        call=lambda callback, **values: callback(None, **values),
    )
    monkeypatch.setattr(me, "_decode_record", lambda record: record)
    return store, repository


def record(identifier, *, status="revoked", owner="owner"):
    return {
        "owner_id": owner,
        "namespace": me.MEMBER_NAMESPACE,
        "member_id": identifier,
        "status": status,
        "device_key_fingerprint": "same-key",
    }


def test_active_identity_beyond_revoked_history_refuses_partial_result(monkeypatch):
    history = [record(str(index)) for index in range(1000)]
    store, repository = store_for(
        monkeypatch, history + [record("active", status="active")]
    )
    with pytest.raises(me.MeshInventoryIncomplete) as refusal:
        store._active_key_holder(None, "owner", "same-key")
    assert refusal.value.status == 503
    assert refusal.value.code == "mesh_inventory_incomplete"
    assert repository.calls == [("owner", me.MEMBER_NAMESPACE, 1000)]


@pytest.mark.parametrize("count", [0, 998])
def test_complete_inventory_preserves_owner_identity_and_exclusion(monkeypatch, count):
    store, _ = store_for(
        monkeypatch,
        [record(str(index)) for index in range(count)]
        + [record("active", status="active")]
        + [record("foreign", status="active", owner="other")],
    )
    assert store._active_key_holder(None, "owner", "same-key")["member_id"] == "active"
    assert store._active_key_holder(None, "owner", "different-key") is None
    assert (
        store._active_key_holder(None, "owner", "same-key", exclude_member_id="active")
        is None
    )


def test_inventory_display_refuses_saturated_owner_read(monkeypatch):
    store, _ = store_for(monkeypatch, [record(str(index)) for index in range(1000)])
    with pytest.raises(me.MeshInventoryIncomplete):
        store._list("owner", me.MEMBER_NAMESPACE)
    assert store._list("other", me.MEMBER_NAMESPACE) == []
