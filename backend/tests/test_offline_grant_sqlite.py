"""Runs the offline-grant lookups that machine authority depends on against a personal node's SQLite Plane, with no PostgreSQL reachable.
Uses orchestrator/offline_grant.py over the pinned AstralPlane SQLite backend composed by orchestrator/plane_composition.py."""

from __future__ import annotations

import time
from uuid import uuid4

from orchestrator.offline_grant import OfflineGrantStore
from orchestrator.plane_composition import compose_plane_from_environment
from tests.test_plane_sqlite_selection import MANIFEST, _environment, _forbid_postgres

DAY_MS = 86_400_000


def test_offline_grant_lookups_run_on_the_personal_sqlite_backend(tmp_path, monkeypatch):
    _forbid_postgres(monkeypatch)
    plane = compose_plane_from_environment(MANIFEST, environ=_environment(tmp_path))
    try:
        assert plane.contract.backend == "sqlite"
        store = OfflineGrantStore(plane_runtime=plane.runtime, plane_repositories=plane.repositories)
        repository = store._grants.repository
        now = int(time.time() * 1000)
        ids = {name: str(uuid4()) for name in ("any", "agent", "older", "expired", "revoked", "other")}
        rows = {
            "older": ("owner", None, now - 3 * DAY_MS, now + DAY_MS),
            "agent": ("owner", "agent-1", now - 2 * DAY_MS, now + DAY_MS),
            "any": ("owner", None, now - DAY_MS, now + DAY_MS),
            "expired": ("owner", None, now - 4 * DAY_MS, now - 1),
            "revoked": ("owner", None, now - 1000, now + DAY_MS),
            "other": ("other-owner", None, now - 10, now + DAY_MS),
        }
        with store._grants.transaction() as transaction:
            for name, (owner, agent, issued, expires) in rows.items():
                repository.create_grant(
                    transaction, grant_id=ids[name], owner_id=owner, agent_id=agent,
                    encrypted_refresh_token=b"opaque", issued_at=issued, expires_at=expires,
                )
            repository.revoke_grant(transaction, owner_id="owner", grant_id=ids["revoked"], revoked_at=now)
        assert store.latest_valid_for("owner") == ids["any"]
        assert store.latest_valid_for("owner", "agent-1") == ids["agent"]
        assert store.latest_valid_for("owner", "agent-2") == ids["any"]
        assert store.latest_valid_for("nobody") is None
        assert store.is_valid(ids["any"], user_id="owner") is True
        assert store.is_valid(ids["expired"], user_id="owner") is False
        assert store.is_valid(ids["revoked"], user_id="owner") is False
        assert store.is_valid(ids["other"], user_id="owner") is False
    finally:
        plane.close()
