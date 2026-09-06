"""Focused contract tests for Deep's typed AstralPlane repository adapters."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

from cryptography.fernet import Fernet

from astralplane import create_repository_catalog
from astralplane.repositories.chat_steps import ChatStepRecord, ChatStepStatus
from astralplane.repositories.agents import AgentOwnershipRecord
from astralplane.repositories.credentials import (
    CredentialRecord,
    MachineCredentialRecord,
)
from astralplane.repositories.offline_grants import (
    OfflineGrantRecord,
    OfflineGrantReference,
)
from astralplane.repositories.remote import RemoteMachine
from astralplane.repositories.share_grants import ShareGrantRecord
from orchestrator import remote_machines
from orchestrator.api import list_agents, set_agent_visibility
from orchestrator.artifact_share import ShareGrantStore, hash_token
from orchestrator.chat_steps import ChatStepRecorder
from orchestrator.credential_manager import CredentialManager
from orchestrator.offline_grant import OfflineGrantStore
from orchestrator.tool_permissions import ToolPermissionManager
from orchestrator.models import AgentVisibilityRequest
from shared.protocol import AgentCard, AgentSkill


class _Database:
    def __init__(self, **repositories) -> None:
        self.plane_repositories = SimpleNamespace(**repositories)
        self.plane_runtime = SimpleNamespace(
            repositories=self.plane_repositories,
            transaction=self._transaction,
        )

    @contextmanager
    def _transaction(self):
        yield object()


class _Credentials:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str, str], CredentialRecord] = {}
        self.machines: dict[tuple[str, str], MachineCredentialRecord] = {}
        self.sequence = 0

    def upsert_credential(self, _transaction, **values):
        self.sequence += 1
        key = (values["owner_id"], values["agent_id"], values["credential_key"])
        previous = self.values.get(key)
        record = CredentialRecord(
            credential_id=(
                previous.credential_id if previous is not None else self.sequence
            ),
            owner_id=values["owner_id"],
            agent_id=values["agent_id"],
            credential_key=values["credential_key"],
            encrypted_value=values["encrypted_value"],
            created_at=(
                previous.created_at if previous is not None else values["updated_at"]
            ),
            updated_at=values["updated_at"],
        )
        self.values[key] = record
        return record

    def get_credential(self, _transaction, **values):
        return self.values.get(
            (values["owner_id"], values["agent_id"], values["credential_key"])
        )

    def list_credentials(self, _transaction, **values):
        return tuple(
            record
            for key, record in sorted(self.values.items())
            if key[:2] == (values["owner_id"], values["agent_id"])
        )[: values["limit"]]

    def list_credential_keys(self, _transaction, **values):
        return tuple(
            record.credential_key
            for record in self.list_credentials(_transaction, **values)
        )

    def delete_credential(self, _transaction, **values):
        return (
            self.values.pop(
                (
                    values["owner_id"],
                    values["agent_id"],
                    values["credential_key"],
                ),
                None,
            )
            is not None
        )

    def delete_agent_credentials(self, _transaction, **values):
        keys = [
            key
            for key in self.values
            if key[:2] == (values["owner_id"], values["agent_id"])
        ]
        for key in keys:
            del self.values[key]
        return len(keys)

    def get_machine_credential(self, _transaction, **values):
        return self.machines.get((values["owner_id"], values["machine_id"]))

    def create_machine_credential(self, _transaction, **values):
        record = MachineCredentialRecord(
            machine_id=values["machine_id"],
            owner_id=values["owner_id"],
            credential_type=values["credential_type"],
            encrypted_secret=values["encrypted_secret"],
            encrypted_passphrase=values["encrypted_passphrase"],
            created_at=values["created_at"],
            updated_at=values["created_at"],
        )
        self.machines[(record.owner_id, record.machine_id)] = record
        return record

    def compare_and_set_machine_credential(self, _transaction, **values):
        key = (values["owner_id"], values["machine_id"])
        previous = self.machines[key]
        record = replace(
            previous,
            credential_type=values["credential_type"],
            encrypted_secret=values["encrypted_secret"],
            encrypted_passphrase=values["encrypted_passphrase"],
            updated_at=values["updated_at"],
        )
        self.machines[key] = record
        return record

    def delete_machine_credential(self, _transaction, **values):
        return self.machines.pop((values["owner_id"], values["machine_id"]), None) is not None

    def delete_owner_machine_credentials(self, _transaction, **values):
        keys = [key for key in self.machines if key[0] == values["owner_id"]]
        for key in keys:
            del self.machines[key]
        return len(keys)


class _OfflineGrants:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], OfflineGrantRecord] = {}

    def create_grant(self, _transaction, **values):
        record = OfflineGrantRecord(
            grant_id=values["grant_id"],
            owner_id=values["owner_id"],
            agent_id=values["agent_id"],
            encrypted_refresh_token=values["encrypted_refresh_token"],
            issued_at=values["issued_at"],
            expires_at=values["expires_at"],
            revoked_at=None,
            created_at=values["issued_at"],
            updated_at=values["issued_at"],
        )
        self.values[(record.owner_id, record.grant_id)] = record
        return record

    def get_grant(self, _transaction, **values):
        return self.values.get((values["owner_id"], values["grant_id"]))

    def find_latest_valid(self, _transaction, **values):
        records = [
            record
            for (owner, _), record in self.values.items()
            if owner == values["owner_id"]
            and record.revoked_at is None
            and record.expires_at > values["as_of"]
        ]
        records.sort(
            key=lambda record: (
                record.agent_id == values["agent_id"],
                record.issued_at,
            ),
            reverse=True,
        )
        if not records:
            return None
        record = records[0]
        return OfflineGrantReference(
            grant_id=record.grant_id,
            owner_id=record.owner_id,
            agent_id=record.agent_id,
            issued_at=record.issued_at,
            expires_at=record.expires_at,
        )

    def revoke_owner(self, _transaction, **values):
        count = 0
        for key, record in tuple(self.values.items()):
            if record.owner_id == values["owner_id"] and record.revoked_at is None:
                self.values[key] = replace(
                    record,
                    revoked_at=values["revoked_at"],
                    updated_at=values["revoked_at"],
                )
                count += 1
        return count


class _ChatSteps:
    def __init__(self) -> None:
        self.values: dict[str, ChatStepRecord] = {}

    def create_step(self, _transaction, **values):
        record = ChatStepRecord(
            step_id=values["step_id"],
            conversation_id=values["conversation_id"],
            owner_id=values["owner_id"],
            turn_message_id=values["turn_message_id"],
            kind=values["kind"],
            name=values["name"],
            status=ChatStepStatus.IN_PROGRESS,
            args_truncated=values["args_truncated"],
            args_was_truncated=values["args_was_truncated"],
            result_summary=None,
            result_was_truncated=False,
            error_message=None,
            started_at=values["started_at"],
            ended_at=None,
        )
        self.values[record.step_id] = record
        return record

    def get_step(self, _transaction, **values):
        record = self.values.get(values["step_id"])
        return record if record is not None and record.owner_id == values["owner_id"] else None

    def finish_step(self, _transaction, **values):
        record = self.values[values["step_id"]]
        updated = replace(
            record,
            status=ChatStepStatus(values["status"]),
            result_summary=values["result_summary"],
            result_was_truncated=values["result_was_truncated"],
            error_message=values["error_message"],
            ended_at=values["ended_at"],
        )
        self.values[updated.step_id] = updated
        return updated


class _Remote:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], RemoteMachine] = {}

    def create_machine(self, _transaction, *, machine: RemoteMachine):
        self.values[(machine.owner_id, machine.machine_id)] = machine
        return machine

    def get_machine(self, _transaction, **values):
        return self.values.get((values["owner_id"], values["machine_id"]))

    def resolve_machine(self, _transaction, **values):
        reference = values["reference"].lower()
        return next(
            (
                machine
                for (owner, _), machine in self.values.items()
                if owner == values["owner_id"]
                and reference
                in {
                    machine.machine_id.lower(),
                    machine.label.lower(),
                    machine.address.lower(),
                }
            ),
            None,
        )

    def list_machines(self, _transaction, **values):
        return tuple(
            sorted(
                (
                    machine
                    for (owner, _), machine in self.values.items()
                    if owner == values["owner_id"]
                ),
                key=lambda machine: (machine.label, machine.machine_id),
            )
        )[: values["limit"]]

    def delete_machine(self, _transaction, **values):
        return self.values.pop((values["owner_id"], values["machine_id"]), None) is not None

    def record_probe(self, _transaction, **values):
        key = (values["owner_id"], values["machine_id"])
        machine = self.values[key]
        updated = replace(
            machine,
            last_verdict=values["verdict"],
            last_checked_at=values["checked_at"],
            host_key_type=values["host_key_type"] or machine.host_key_type,
            host_key_fingerprint=(
                values["host_key_fingerprint"] or machine.host_key_fingerprint
            ),
            host_key_blob=values["host_key_blob"] or machine.host_key_blob,
            updated_at=values["checked_at"],
        )
        self.values[key] = updated
        return updated

    def clear_host_trust(self, _transaction, **values):
        key = (values["owner_id"], values["machine_id"])
        updated = replace(
            self.values[key],
            host_key_type=None,
            host_key_fingerprint=None,
            host_key_blob=None,
            updated_at=values["updated_at"],
        )
        self.values[key] = updated
        return updated


class _ShareGrants:
    def __init__(self, token: str) -> None:
        now = datetime.now(UTC)
        self.record = ShareGrantRecord(
            share_id=7,
            token_sha256=hash_token(token),
            owner_id="alice",
            chat_id="chat-a",
            scope="canvas",
            component_id=None,
            snapshot_html="<main>safe</main>",
            snapshot_json=({"type": "text", "text": "safe"},),
            created_at=now,
            expires_at=None,
            revoked_at=None,
            open_count=0,
        )
        self.live = True

    def resolve_active_by_digest(self, _transaction, **values):
        if self.live and values["token_sha256"] == self.record.token_sha256:
            return self.record
        return None

    def record_open(self, _transaction, **values):
        if (
            not self.live
            or values["share_id"] != self.record.share_id
            or values["token_sha256"] != self.record.token_sha256
        ):
            return None
        self.record = replace(self.record, open_count=self.record.open_count + 1)
        return self.record


class _ToolPolicy:
    def __init__(self) -> None:
        self.disabled: dict[str, list[str]] = {}

    def list_disabled_agents(self, _transaction, *, owner_id):
        return tuple(self.disabled.get(owner_id, ()))

    def list_overrides(self, _transaction, *, owner_id, agent_id):
        return ()

    def list_scopes(self, _transaction, *, owner_id, agent_id):
        return ()

    def set_agent_disabled(
        self,
        _transaction,
        *,
        owner_id,
        agent_id,
        disabled,
        updated_at,
    ):
        assert updated_at >= 0
        values = self.disabled.setdefault(owner_id, [])
        present = agent_id in values
        if present == disabled:
            return False
        if disabled:
            values.append(agent_id)
        else:
            values.remove(agent_id)
        return True

    def get_tool_selection(self, _transaction, *, owner_id, agent_id):
        return getattr(self, "selections", {}).get((owner_id, agent_id))

    def set_tool_selection(
        self,
        _transaction,
        *,
        owner_id,
        agent_id,
        selected_tools,
        updated_at,
    ):
        assert updated_at >= 0
        self.selections = getattr(self, "selections", {})
        self.selections[(owner_id, agent_id)] = tuple(selected_tools)
        return tuple(selected_tools)

    def clear_tool_selection(
        self,
        _transaction,
        *,
        owner_id,
        agent_id,
        updated_at,
    ):
        assert updated_at >= 0
        self.selections = getattr(self, "selections", {})
        return self.selections.pop((owner_id, agent_id), None) is not None


class _AgentAdministration:
    def __init__(self) -> None:
        self.ownership = {
            "agent-a": AgentOwnershipRecord(
                agent_id="agent-a",
                owner_email="alice@example.test",
                is_public=False,
                created_at=1,
                updated_at=1,
            )
        }
        self.user_agents = {}
        self.trust = {}

    def list_ownership_for_administration(self, _transaction, *, limit):
        assert limit == 5000
        return tuple(self.ownership.values())

    def get_ownership(self, _transaction, *, agent_id):
        return self.ownership.get(agent_id)

    def get_agent_for_administration(self, _transaction, *, agent_id):
        return self.user_agents.get(agent_id)

    def get_trust(self, _transaction, *, agent_id):
        return self.trust.get(agent_id)

    def set_visibility(
        self,
        _transaction,
        *,
        agent_id,
        owner_email,
        is_public,
        updated_at,
    ):
        prior = self.ownership[agent_id]
        assert owner_email == prior.owner_email
        assert updated_at >= prior.updated_at
        updated = replace(prior, is_public=is_public, updated_at=updated_at)
        self.ownership[agent_id] = updated
        return updated


class _Runtime:
    @contextmanager
    def transaction(self):
        yield object()

def test_credential_adapter_preserves_owner_scope(monkeypatch) -> None:
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    repository = _Credentials()
    manager = CredentialManager(
        db=_Database(credentials=repository),
        plane_repository=repository,
    )

    manager.set_credential("alice", "agent-a", "TOKEN", "secret", e2e=False)
    assert manager.get_credential("alice", "agent-a", "TOKEN") == "secret"
    assert manager.get_credential("bob", "agent-a", "TOKEN") is None
    assert manager.list_credential_keys("alice", "agent-a") == ["TOKEN"]


def test_offline_grant_adapter_requires_owner_for_validity(monkeypatch) -> None:
    key = Fernet.generate_key().decode()
    monkeypatch.setattr("orchestrator.offline_grant.OFFLINE_GRANT_ENC_KEY", key)
    repository = _OfflineGrants()
    store = OfflineGrantStore(
        db=_Database(offline_grants=repository),
        plane_repository=repository,
    )
    monkeypatch.setattr(store, "_session_reference", lambda owner, token: {
        "session_id": "session-a", "created_at": 1, "interactive_anchor": 1,
    })

    grant_id = store.capture("alice", "refresh", "agent-a")
    assert store.is_valid(grant_id, user_id="alice") is True
    assert store.is_valid(grant_id, user_id="bob") is False
    assert store.latest_valid_for("alice", "agent-a") == grant_id
    assert store.revoke_for_user("alice") == 1
    assert store.is_valid(grant_id, user_id="alice") is False


def test_chat_step_adapter_emits_canonical_plane_state() -> None:
    repository = _ChatSteps()
    sent: list[dict] = []

    async def safe_send(_websocket, payload: str) -> None:
        sent.append(json.loads(payload))

    recorder = ChatStepRecorder(
        db=_Database(chat_steps=repository),
        websocket=object(),
        safe_send=safe_send,
        chat_id="chat-a",
        user_id="alice",
        plane_repository=repository,
    )
    step_id = asyncio.run(recorder.start("tool_call", "lookup", {"q": "safe"}))
    asyncio.run(recorder.complete(step_id, {"ok": True}))

    assert repository.values[step_id].status is ChatStepStatus.COMPLETED
    assert [event["step"]["status"] for event in sent] == [
        "in_progress",
        "completed",
    ]


def test_remote_machine_adapter_uses_typed_owner_scoped_repository() -> None:
    repository = _Remote()
    database = _Database(remote=repository)
    machine_id = remote_machines.create_machine(
        database,
        "alice",
        "dgx",
        "10.0.0.5",
        22,
        "astral",
        "linux",
        "cluster",
    )

    assert remote_machines.get_machine(database, "bob", machine_id) is None
    assert remote_machines.resolve_machine(database, "alice", "DGX")[
        "machine_id"
    ] == machine_id
    remote_machines.record_probe(
        database,
        "alice",
        machine_id,
        "ok",
        {
            "type": "ssh-ed25519",
            "fingerprint": "SHA256:test",
            "blob_b64": "opaque",
        },
    )
    assert remote_machines.get_machine(database, "alice", machine_id)[
        "host_key_fingerprint"
    ] == "SHA256:test"
    remote_machines.retrust_host_key(database, "alice", machine_id)
    assert remote_machines.get_machine(database, "alice", machine_id)[
        "host_key_fingerprint"
    ] is None


def test_share_adapter_fences_public_open_against_revocation(monkeypatch) -> None:
    token = "public-token"
    repository = _ShareGrants(token)
    store = ShareGrantStore(
        db=_Database(share_grants=repository),
        plane_repository=repository,
    )
    audit_events: list[dict] = []

    async def record_event(**values) -> None:
        audit_events.append(values)

    monkeypatch.setattr(
        "orchestrator.artifact_share.record_share_event",
        record_event,
    )

    grant = asyncio.run(store.resolve(token))
    assert grant is not None
    repository.live = False
    assert asyncio.run(store.record_open(grant)) is False
    assert repository.record.open_count == 0
    assert audit_events == []


def test_tool_policy_adapter_owns_disabled_agent_preferences() -> None:
    repository = _ToolPolicy()
    manager = ToolPermissionManager(
        db=_Database(
            tool_policy_state=repository,
            agents=create_repository_catalog().agents,
        ),
        plane_repository=repository,
    )

    assert manager.list_disabled_agents("alice") == ()
    assert manager.set_agent_disabled("alice", "agent-a", True) is True
    assert manager.is_agent_disabled("alice", "agent-a") is True
    assert manager.set_agent_disabled("alice", "agent-a", True) is False
    assert manager.set_agent_disabled("alice", "agent-a", False) is True
    assert manager.list_disabled_agents("alice") == ()

    assert manager.get_tool_selection("alice", "agent-a") is None
    assert manager.set_tool_selection(
        "alice", "agent-a", ["read", "summarize"]
    ) == ["read", "summarize"]
    assert manager.get_tool_selection("alice", "agent-a") == ["read", "summarize"]
    assert manager.clear_tool_selection("alice", "agent-a") is True
    assert manager.clear_tool_selection("alice", "agent-a") is False


def test_tool_permission_manager_resolves_the_public_plane_catalog_key() -> None:
    runtime = SimpleNamespace(repositories=create_repository_catalog())

    manager = ToolPermissionManager(plane_runtime=runtime)

    assert manager._policy.repository is runtime.repositories.tool_policy_state


def test_tool_permission_manager_uses_typed_agent_isolation_and_ownership() -> None:
    policy = _ToolPolicy()
    agents = _AgentAdministration()
    agents.user_agents["agent-a"] = SimpleNamespace(
        owner_id="alice",
        deleted_at=None,
    )
    manager = ToolPermissionManager(
        db=_Database(tool_policy_state=policy, agents=agents),
        plane_repository=policy,
        agent_repository=agents,
    )
    manager.register_tool_scopes("agent-a", {"read": "tools:read"})

    assert manager.is_tool_allowed("alice", "agent-a", "read") is True
    assert manager.is_tool_allowed("bob", "agent-a", "read") is False
    assert manager._safe_flip_allowed("agent-a") is False
    assert manager._safe_flip_allowed("unowned-built-in") is True


def test_agent_rest_projection_and_visibility_use_one_plane_boundary() -> None:
    agents = _AgentAdministration()
    policy = _ToolPolicy()
    policy.disabled["alice"] = ["agent-a"]
    repositories = SimpleNamespace(agents=agents, tool_policy_state=policy)
    runtime = _Runtime()
    card = AgentCard(
        name="Agent A",
        description="typed",
        agent_id="agent-a",
        skills=[
            AgentSkill(
                id="read",
                name="Read",
                description="read",
                scope="tools:read",
            )
        ],
    )
    orch = SimpleNamespace(
        agent_cards={"agent-a": card},
        security_flags={},
        _is_draft_agent=lambda _agent_id: False,
        runtime_composition=SimpleNamespace(
            plane=SimpleNamespace(runtime=runtime, repositories=repositories)
        ),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(orchestrator=orch))
    )

    projection = asyncio.run(list_agents(request, user_id="alice"))
    assert projection.agents[0].disabled is True
    assert projection.agents[0].owner_email == "alice@example.test"

    updated = asyncio.run(
        set_agent_visibility(
            request,
            "agent-a",
            AgentVisibilityRequest(is_public=True),
            payload={"email": "alice@example.test"},
            user_id="alice",
        )
    )
    assert updated == {"agent_id": "agent-a", "is_public": True}
    assert agents.ownership["agent-a"].is_public is True
