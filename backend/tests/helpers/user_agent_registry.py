"""In-memory Plane repository fixture for Deep user-agent policy tests."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import threading
from types import SimpleNamespace

from astralplane.repositories import RepositoryConflictError
from astralplane.repositories.agents import UserAgentRecord
from orchestrator.user_agents import UserAgentRegistry


def _record(**overrides) -> UserAgentRecord:
    values = {
        "agent_id": "agent-a",
        "owner_id": "owner-a",
        "owner_email": None,
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


class InMemoryAgentRepository:
    def __init__(self) -> None:
        self.records: dict[str, UserAgentRecord] = {}
        self.locked_owner_ids: list[str] = []
        self.compare_and_set_calls = 0

    def lock_owner(self, _transaction, *, owner_id):
        self.locked_owner_ids.append(owner_id)

    def get_agent_for_administration(
        self,
        _transaction,
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
        self.compare_and_set_calls += 1
        current = self.records.get(agent_id)
        if (
            current is None
            or current.owner_id != owner_id
            or current.deleted_at is not None
            or current.state_revision != expected_revision
        ):
            raise RepositoryConflictError("stale agent")
        normalized = dict(updates)
        for name in ("declared_tools", "declared_scopes", "declared_egress"):
            if name in normalized and normalized[name] is not None:
                normalized[name] = tuple(normalized[name])
        updated = replace(
            current,
            **normalized,
            state_revision=current.state_revision + 1,
        )
        self.records[agent_id] = updated
        return updated

    def list_agents(self, _transaction, *, owner_id, include_deleted, limit):
        rows = [
            record
            for record in self.records.values()
            if record.owner_id == owner_id
            and (include_deleted or record.deleted_at is None)
        ]
        rows.sort(key=lambda record: (-(record.updated_at or 0), record.agent_id))
        return tuple(rows[:limit])

    def upsert_ownership(self, _transaction, **_values):
        return None

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
    def __init__(self, repository: InMemoryAgentRepository) -> None:
        self.repositories = SimpleNamespace(agents=repository)
        # Every test owner currently shares one transaction lock. This mirrors
        # the production registry's owner lock: the read plus revision-CAS (or
        # tombstone) is one serializable unit, while callers may still race to
        # acquire it from separate threads.
        self._transaction_lock = threading.RLock()

    @contextmanager
    def transaction(self):
        with self._transaction_lock:
            yield object()


def make_user_agent_registry() -> UserAgentRegistry:
    registry, _repository = make_user_agent_registry_with_repository()
    return registry


def make_user_agent_registry_with_repository(
) -> tuple[UserAgentRegistry, InMemoryAgentRepository]:
    repository = InMemoryAgentRepository()
    runtime = _Runtime(repository)
    return (
        UserAgentRegistry(
            plane_runtime=runtime,
            plane_repositories=runtime.repositories,
        ),
        repository,
    )
