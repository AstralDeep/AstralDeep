"""Focused Plane-boundary tests for Deep's user-agent registry policy."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from astralplane.repositories import RepositoryConflictError
from astralplane.repositories.agents import UserAgentRecord
from orchestrator import user_agents


def _record(**overrides) -> UserAgentRecord:
    values = {
        "agent_id": "agent-a",
        "owner_id": "alice",
        "owner_email": "alice@example.test",
        "display_name": "Agent A",
        "status": "authoring",
        "declared_tools": (),
        "declared_scopes": (),
        "declared_egress": None,
        "constitution_version": None,
        "validated_at": None,
        "revalidation_required": False,
        "draft_id": None,
        "host_client_id": None,
        "host_session_id": None,
        "host_last_seen_at": None,
        "is_public": False,
        "deleted_at": None,
        "created_at": 1,
        "updated_at": 1,
        "active_revision_id": None,
        "last_known_good_revision_id": None,
        "selected_host_session_id": None,
        "authoritative_instance_id": None,
        "lifecycle_generation": 0,
        "generation_counter": 0,
        "state_revision": 0,
        "validated_policy_revision": None,
    }
    values.update(overrides)
    return UserAgentRecord(**values)


class _AgentRepository:
    def __init__(self) -> None:
        self.records: dict[str, UserAgentRecord] = {}
        self.locked: list[tuple[object, str]] = []
        self.ownership: list[tuple[object, str, str, bool]] = []
        self.ownership_records: dict[str, SimpleNamespace] = {}
        self.trust_records: dict[str, SimpleNamespace] = {}

    def lock_owner(self, transaction, *, owner_id):
        self.locked.append((transaction, owner_id))

    def get_agent_for_administration(
        self,
        _query,
        *,
        agent_id,
        for_update=False,
    ):
        del for_update
        return self.records.get(agent_id)

    def create_agent(self, _transaction, **values):
        if values["agent_id"] in self.records:
            raise RepositoryConflictError("duplicate agent")
        record = _record(
            agent_id=values["agent_id"],
            owner_id=values["owner_id"],
            owner_email=values["owner_email"],
            display_name=values["display_name"],
            declared_tools=tuple(values["declared_tools"]),
            declared_scopes=tuple(values["declared_scopes"]),
            declared_egress=(
                None
                if values["declared_egress"] is None
                else tuple(values["declared_egress"])
            ),
            draft_id=values["draft_id"],
            created_at=values["observed_at"],
            updated_at=values["observed_at"],
        )
        self.records[record.agent_id] = record
        return record

    def compare_and_set_agent(
        self,
        _transaction,
        *,
        owner_id,
        agent_id,
        expected_revision,
        updates,
    ):
        current = self.records.get(agent_id)
        if (
            current is None
            or current.owner_id != owner_id
            or current.deleted_at is not None
            or current.state_revision != expected_revision
        ):
            raise RepositoryConflictError("stale agent")
        normalized = dict(updates)
        for key in ("declared_tools", "declared_scopes", "declared_egress"):
            if key in normalized and normalized[key] is not None:
                normalized[key] = tuple(normalized[key])
        updated = replace(
            current,
            **normalized,
            state_revision=current.state_revision + 1,
        )
        self.records[agent_id] = updated
        return updated

    def list_agents(self, _query, *, owner_id, include_deleted, limit):
        rows = [
            record
            for record in self.records.values()
            if record.owner_id == owner_id
            and (include_deleted or record.deleted_at is None)
        ]
        rows.sort(key=lambda record: (-(record.updated_at or 0), record.agent_id))
        return tuple(rows[:limit])

    def upsert_ownership(
        self,
        transaction,
        *,
        agent_id,
        owner_email,
        is_public,
        observed_at,
    ):
        existing = self.ownership_records.get(agent_id)
        if existing is not None and existing.owner_email != owner_email:
            raise RepositoryConflictError("ownership conflict")
        created_at = observed_at if existing is None else existing.created_at
        record = SimpleNamespace(
            agent_id=agent_id,
            owner_email=owner_email,
            is_public=is_public,
            created_at=created_at,
            updated_at=observed_at,
        )
        self.ownership_records[agent_id] = record
        self.ownership.append(
            (transaction, agent_id, owner_email, is_public)
        )
        return record

    def get_ownership(self, _query, *, agent_id):
        return self.ownership_records.get(agent_id)

    def list_ownership_for_administration(self, _query, *, limit):
        return tuple(
            self.ownership_records[key]
            for key in sorted(self.ownership_records)[:limit]
        )

    def set_visibility(
        self,
        _transaction,
        *,
        agent_id,
        owner_email,
        is_public,
        updated_at,
    ):
        current = self.ownership_records[agent_id]
        assert current.owner_email == owner_email
        record = SimpleNamespace(
            agent_id=agent_id,
            owner_email=owner_email,
            is_public=is_public,
            created_at=current.created_at,
            updated_at=updated_at,
        )
        self.ownership_records[agent_id] = record
        return record

    def get_trust(self, _query, *, agent_id):
        return self.trust_records.get(agent_id)

    def set_trust(
        self,
        _transaction,
        *,
        agent_id,
        is_safe,
        marked_by,
        reset_for_revision=False,
    ):
        current = self.trust_records.get(agent_id)
        record = SimpleNamespace(
            agent_id=agent_id,
            is_safe=is_safe,
            marked_by=marked_by,
            prior_state=None if current is None else current.is_safe,
            reset_for_revision=reset_for_revision,
        )
        self.trust_records[agent_id] = record
        return record

    def tombstone_agent(
        self,
        transaction,
        *,
        owner_id,
        agent_id,
        expected_revision,
        deleted_at,
    ):
        return self.compare_and_set_agent(
            transaction,
            owner_id=owner_id,
            agent_id=agent_id,
            expected_revision=expected_revision,
            updates={
                "status": "disabled",
                "deleted_at": deleted_at,
                "updated_at": deleted_at,
            },
        )


class _Runtime:
    def __init__(self, repository: _AgentRepository) -> None:
        self.repositories = SimpleNamespace(agents=repository)
        self.transactions: list[object] = []

    @contextmanager
    def transaction(self):
        transaction = object()
        self.transactions.append(transaction)
        yield transaction


def _registry():
    repository = _AgentRepository()
    runtime = _Runtime(repository)
    return (
        user_agents.UserAgentRegistry(
            plane_runtime=runtime,
            plane_repositories=runtime.repositories,
        ),
        repository,
        runtime,
    )


def test_registry_uses_typed_plane_lifecycle_and_one_live_transaction() -> None:
    registry, repository, runtime = _registry()

    user_agents.create_user_agent(
        registry,
        agent_id="agent-a",
        owner_user_id="alice",
        owner_email="alice@example.test",
        display_name="Agent A",
        declared_tools=["lookup"],
        declared_scopes=["tools:read"],
    )
    user_agents.mark_validated(registry, "agent-a", "constitution-v1")
    assert user_agents.authorize_registration(registry, "alice", "agent-a") == (
        True,
        "",
    )

    before = len(runtime.transactions)
    user_agents.go_live(
        registry,
        "agent-a",
        host_client_id="desktop-a",
        host_session_id="session-a",
    )
    assert len(runtime.transactions) == before + 1
    assert repository.ownership[-1][0] is runtime.transactions[-1]
    row = user_agents.get_user_agent(registry, "agent-a")
    assert row is not None
    assert row["status"] == "live"
    assert row["declared_tools"] == ["lookup"]
    assert user_agents.can_user_use_agent(registry, "alice", "agent-a") is True
    assert user_agents.can_user_use_agent(registry, "bob", "agent-a") is False


def test_registry_fences_identity_revalidation_and_tombstones() -> None:
    registry, repository, _runtime = _registry()
    user_agents.create_user_agent(
        registry,
        agent_id="agent-a",
        owner_user_id="alice",
        display_name="Agent A",
    )

    with pytest.raises(user_agents.UserAgentOwnershipConflict):
        user_agents.create_user_agent(
            registry,
            agent_id="agent-a",
            owner_user_id="bob",
            display_name="Stolen identity",
        )

    user_agents.mark_validated(registry, "agent-a", "constitution-v1")
    user_agents.mark_revalidation_required(registry, "agent-a")
    allowed, reason = user_agents.authorize_registration(
        registry,
        "alice",
        "agent-a",
    )
    assert allowed is False
    assert "re-pass Analyze" in reason

    user_agents.soft_delete(registry, "agent-a")
    assert repository.records["agent-a"].deleted_at is not None
    assert user_agents.list_user_agents(registry, "alice") == []
    assert user_agents.can_user_use_agent(registry, "alice", "agent-a") is False
    with pytest.raises(user_agents.AgentDeletedError):
        user_agents.soft_delete(registry, "agent-a")


def test_non_user_agents_preserve_the_normal_permission_path() -> None:
    registry, _repository, _runtime = _registry()
    assert user_agents.can_user_use_agent(registry, "alice", "built-in") is True
    assert user_agents.authorize_registration(
        registry,
        "alice",
        "__reserved",
    ) == (False, "reserved agent id")


def test_registry_projects_plane_ownership_and_trust_without_database_facade() -> None:
    registry, _repository, _runtime = _registry()

    ownership = registry.set_agent_ownership(
        "general-1",
        "operator@example.test",
        is_public=True,
    )
    assert ownership["owner_email"] == "operator@example.test"
    assert registry.get_agent_ownership("general-1")["is_public"] is True
    assert registry.set_agent_visibility("general-1", False) is True
    assert registry.get_all_agent_ownership() == [
        registry.get_agent_ownership("general-1")
    ]

    assert registry.get_agent_is_safe("general-1") is False
    assert registry.upsert_agent_safe(
        "general-1",
        True,
        marked_by="system",
    ) is False
    assert registry.get_agent_is_safe("general-1") is True
    assert registry.reset_agent_safe(
        "general-1",
        marked_by="system",
    ) is True
    assert registry.get_agent_is_safe("general-1") is False

    with pytest.raises(user_agents.UserAgentOwnershipConflict):
        registry.set_agent_ownership(
            "general-1",
            "attacker@example.test",
            is_public=True,
        )
