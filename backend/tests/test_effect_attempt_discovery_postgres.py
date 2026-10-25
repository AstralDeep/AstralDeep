"""Exercises owner-only attempt discovery and late attestation through mounted application APIs.
Real Plane, signed local IAM and the qualified SSH adapter retain lost-reply evidence; only SSH is substituted.
"""

import asyncio
from datetime import datetime, timezone
import re
from types import SimpleNamespace
import uuid

import httpx
import pytest

from orchestrator import remote_machines, web_auth
from orchestrator.api import async_task_router
from orchestrator.connection_context import _CONNECTION_OPERATION_CONTEXT
from orchestrator.credential_manager import CredentialManager
from orchestrator.remote_effect_execution import selected_effect, select_effect, run_qualified
from orchestrator.remote_transport import ParamikoTransport
from orchestrator.work_admission import OperationState, OwnerScope
from orchestrator.work_reconciliation import EFFECT_ATTEMPT_NAMESPACE, EffectReconciliationService
from tests.test_human_request_authority_088 import human as human
from tests.test_work_control_authority_088 import (
    bound as bound, fixture as fixture, runtime as runtime, service as service, signing_key as signing_key,
)
from tests.test_work_reconciliation_postgres import _node, _ledger, _submit_and_claim, _target_authority

pytestmark = pytest.mark.asyncio


class LostReplyClient:
    def __init__(self):
        self.commands = []

    def exec_command(self, command, *, timeout):
        self.commands.append(command)
        raise ConnectionError("reply lost after send")

    def close(self):
        pass


class Socket:
    def __init__(self, cookie):
        self.scope = {"headers": [(b"cookie", cookie.encode())]}


@pytest.fixture
async def discovery(human, fixture, runtime, bound, monkeypatch):
    owner = fixture[1]
    source = SimpleNamespace(plane_runtime=runtime, plane_repositories=runtime.repositories)
    credentials = CredentialManager(db=source)
    machine = remote_machines.create_machine(source, owner, "lost reply", "ssh.test", 22, "runner", "linux", "plain")
    credentials.set_machine_credential(machine, owner, "password", "fixture-ssh-secret")
    remote_machines.record_probe(source, owner, machine, "ok", {
        "type": "ssh-ed25519", "fingerprint": "SHA256:fixture", "blob_b64": "fixture"})
    node = _node(runtime, lambda: datetime.now(timezone.utc))
    accepted, claim = _submit_and_claim(node, uuid.uuid4().hex, owner)
    with runtime.transaction() as tx:
        runtime.repositories.stop_epochs.bind_operation(tx, owner_id=owner, operation_id=accepted.operation_id)
    ledger = _ledger(runtime, node)
    node.set_completion_guard(ledger.completion_uncertain)
    orch = human[2]
    cookie = "astral_session=" + web_auth._sign(fixture[2])
    socket = Socket(cookie)
    connection = SimpleNamespace(websocket=socket, registered=True, closing=False, connection_generation=1)
    orch.effect_attempts, orch.work_admission = ledger, node
    orch.effect_reconciliation = EffectReconciliationService(ledger=ledger, target_authority=_target_authority(runtime))
    orch.ui_sessions = {socket: {"sub": owner, "_raw_token": fixture[3]()}}
    orch._connection_contexts = {id(socket): connection}
    orch.local_agents = {"remote-compute-1": object()}
    context = _CONNECTION_OPERATION_CONTEXT.set({"execution_fence": claim.fence,
        "owner": SimpleNamespace(owner_scope=OwnerScope.USER, owner_user_id=owner)})
    try:
        selected = await select_effect(orch, socket, "remote-compute-1", "make_directory",
            {"machine_id": machine, "path": "/fixture-private-path"}, owner)
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(context)
    ssh = LostReplyClient()
    transport = ParamikoTransport()
    monkeypatch.setattr(transport, "_connect", lambda target, timeout: (ssh, None))
    target = remote_machines.build_effect_target(source, credentials, owner, machine)
    with selected_effect(selected):
        result = await asyncio.to_thread(run_qualified, transport, target,
            ["mkdir", "-p", "/fixture-private-path"], tool="make_directory", owner_id=owner, timeout=1)
    assert not result.ok and len(ssh.commands) == 1
    record = node.terminalize(claim.fence, state=OperationState.COMPLETED,
        terminal_code=None, safe_summary="Completed", retry_after_ms=None)
    assert record.state is OperationState.RETRYABLE and record.terminal_code == "effect_uncertain"
    bound[1].include_router(async_task_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bound[1]), base_url="https://app.invalid") as client:
        yield SimpleNamespace(client=client, path=f"/api/async-tasks/{accepted.operation_id}",
            headers={"Authorization": "Bearer " + fixture[3](), "Cookie": cookie, "Origin": "https://app.invalid"},
            owner=owner, machine=machine, credentials=credentials, ledger=ledger, node=node,
            operation_id=accepted.operation_id, source=source, ssh=ssh, orch=orch)


async def inventory(discovery, **kwargs):
    return await discovery.client.get(discovery.path + "/effect-attempts",
                                      headers=kwargs.pop("headers", discovery.headers), **kwargs)


def report_body(attempt, outcome):
    result = {key: attempt[key] for key in ("attempt_nonce", "argument_digest", "target_kind", "target_id")}
    result["outcome"] = outcome
    if outcome == "confirmed_applied":
        result["result_digest"] = "c" * 64
    return result


async def test_lost_reply_owner_uses_only_discovered_identity_to_refine_and_replay(discovery):
    response = await inventory(discovery)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"operation_id", "execution_generation", "operation_state", "attempts"}
    assert body["operation_id"] == str(discovery.operation_id)
    assert body["execution_generation"] == 1 and body["operation_state"] == "retryable"
    assert len(body["attempts"]) == 1
    attempt = body["attempts"][0]
    assert set(attempt) == {"attempt_nonce", "argument_digest", "target_kind", "target_id",
        "execution_generation", "state", "outcome", "outcome_digest", "recorded_at", "settled_at", "revision"}
    assert re.fullmatch("[0-9a-f]{32}", attempt["attempt_nonce"])
    assert re.fullmatch("[0-9a-f]{64}", attempt["argument_digest"])
    assert attempt["target_kind"] == "remote_machine" and attempt["target_id"] == discovery.machine
    assert attempt["execution_generation"] == 1 and attempt["state"] == "bound" and attempt["outcome"] is None
    assert attempt["settled_at"] is None and attempt["revision"] >= attempt["recorded_at"] > 0
    for outcome in ("uncertain", "confirmed_applied", "confirmed_applied"):
        result = await discovery.client.post(discovery.path + "/effect-reconcile",
            headers=discovery.headers, json=report_body(attempt, outcome))
        assert result.status_code == 200, result.text
        assert result.json()["outcome"] == outcome and result.json()["operation_state"] == "retryable"
    result = await discovery.client.post(discovery.path + "/effect-reconcile",
        headers=discovery.headers, json=report_body(attempt, "confirmed_not_applied"))
    assert result.status_code == 409 and result.json()["detail"] == "reconcile_outcome_conflict"
    final = (await inventory(discovery)).json()["attempts"][0]
    assert final["outcome"] == "confirmed_applied" and final["outcome_digest"] == "c" * 64
    assert final["revision"] > attempt["revision"] and len(discovery.ssh.commands) == 1
    assert discovery.node.repository.get_operation_for_administration(discovery.operation_id).state is OperationState.RETRYABLE


async def test_discovery_supports_cookie_get_without_write_origin_and_does_not_write_audit(discovery, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("discovery must not create an audit record")
    monkeypatch.setattr(discovery.ledger._audit, "insert_in_transaction", forbidden)
    monkeypatch.setattr(discovery.orch.audit_repo, "insert_in_transaction", forbidden)
    response = await inventory(discovery, headers={"Cookie": discovery.headers["Cookie"]})
    assert response.status_code == 200, response.text
    assert len(response.json()["attempts"]) == 1 and len(discovery.ssh.commands) == 1


@pytest.mark.parametrize("kind", ["anonymous", "bearer_only", "query_token"])
async def test_discovery_requires_a_current_selected_human_session(discovery, kind):
    headers = {} if kind != "bearer_only" else {"Authorization": discovery.headers["Authorization"]}
    response = await inventory(discovery, headers=headers,
        params={"token": discovery.headers["Authorization"].removeprefix("Bearer ")} if kind == "query_token" else {})
    assert response.status_code in {401, 403}
    assert "attempt_nonce" not in response.text and "argument_digest" not in response.text


async def test_foreign_current_human_cannot_discover_another_owners_attempt(discovery, fixture):
    owner, sid = str(uuid.uuid4()), uuid.uuid4().hex
    token = fixture[3](sub=owner)
    fixture[0].create(sid, user_id=owner, access_token=token, refresh_token="fixture-other-refresh", hard_max_seconds=3600)
    try:
        response = await inventory(discovery, headers={"Cookie": "astral_session=" + web_auth._sign(sid),
                                                      "Authorization": "Bearer " + token})
        assert response.status_code == 404 and response.json()["detail"] == "work_not_found"
    finally:
        fixture[0].delete(sid)


@pytest.mark.parametrize("identifier,status", [("not-a-uuid", 422), ("00000000-0000-0000-0000-000000000000", 404)])
async def test_discovery_refuses_invalid_and_missing_operations(discovery, identifier, status):
    response = await discovery.client.get(f"/api/async-tasks/{identifier}/effect-attempts", headers=discovery.headers)
    assert response.status_code == status and "attempts" not in response.json()


@pytest.mark.parametrize("change", ["credential", "machine", "incarnation_missing", "incarnation_corrupt",
                                    "owner_incarnation_missing", "owner_incarnation_corrupt"])
async def test_discovery_refuses_retired_targets_without_reinitializing_authority(discovery, runtime, change):
    if change == "credential":
        discovery.credentials.delete_machine_credential(discovery.machine, discovery.owner)
    elif change == "machine":
        remote_machines.delete_machine(discovery.source, discovery.owner, discovery.machine)
    else:
        with runtime.transaction() as tx:
            key = "owner" if change.startswith("owner_") else "machine:" + discovery.machine
            if change.endswith("_missing"):
                runtime.repositories.credentials.delete_credential(tx, owner_id=discovery.owner,
                    agent_id="remote.effect-authority.v1", credential_key=key)
            else:
                runtime.repositories.credentials.upsert_credential(tx, owner_id=discovery.owner,
                    agent_id="remote.effect-authority.v1", credential_key=key,
                    encrypted_value="invalid-ciphertext", updated_at=1)
    with runtime.transaction() as tx:
        before = runtime.repositories.credentials.list_credentials(tx, owner_id=discovery.owner,
            agent_id="remote.effect-authority.v1", limit=128)
    response = await inventory(discovery)
    assert response.status_code == 409 and response.json()["detail"] == "reconcile_target_unavailable"
    with runtime.transaction() as tx:
        after = runtime.repositories.credentials.list_credentials(tx, owner_id=discovery.owner,
            agent_id="remote.effect-authority.v1", limit=128)
    assert after == before and len(discovery.ssh.commands) == 1


@pytest.mark.parametrize("failure", ["corrupt", "overflow", "repository", "operation_repository"])
async def test_discovery_refuses_incomplete_inventory_without_partial_identity(discovery, runtime, monkeypatch, failure):
    if failure in {"corrupt", "overflow"}:
        with runtime.transaction() as tx:
            for _ in range(1 if failure == "corrupt" else 129):
                runtime.repositories.credentials.upsert_credential(tx, owner_id=discovery.owner,
                    agent_id=EFFECT_ATTEMPT_NAMESPACE, credential_key=uuid.uuid4().hex,
                    encrypted_value="invalid-ciphertext", updated_at=1)
    else:
        def unavailable(*args, **kwargs):
            raise RuntimeError("private repository failure")
        monkeypatch.setattr(discovery.ledger._store if failure == "repository" else discovery.node.repository,
            "list" if failure == "repository" else "get_operation_for_administration", unavailable)
    response = await inventory(discovery)
    assert response.status_code == 503 and "attempt_nonce" not in response.text
    assert "private repository failure" not in response.text and len(discovery.ssh.commands) == 1


async def test_discovery_rejects_generation_mismatch(discovery, runtime):
    from orchestrator import mesh_enrollment
    with runtime.transaction() as tx:
        row = runtime.repositories.credentials.list_credentials(tx, owner_id=discovery.owner,
            agent_id=EFFECT_ATTEMPT_NAMESPACE, limit=1)[0]
        value = mesh_enrollment._decode_record(row.encrypted_value)
        value["execution_generation"] += 1
        runtime.repositories.credentials.upsert_credential(tx, owner_id=discovery.owner,
            agent_id=EFFECT_ATTEMPT_NAMESPACE, credential_key=row.credential_key,
            encrypted_value=mesh_enrollment._encode_record(value), updated_at=row.updated_at)
    response = await inventory(discovery)
    assert response.status_code == 409 and response.json()["detail"] == "reconcile_report_stale"


async def test_legacy_attempt_remains_discoverable_under_its_current_target_policy(discovery, runtime):
    from orchestrator import mesh_enrollment
    with runtime.transaction() as tx:
        row = runtime.repositories.credentials.list_credentials(tx, owner_id=discovery.owner,
            agent_id=EFFECT_ATTEMPT_NAMESPACE, limit=1)[0]
        value = mesh_enrollment._decode_record(row.encrypted_value)
        value.update(version=1, dispatch_id=None, authority_digest=None)
        runtime.repositories.credentials.upsert_credential(tx, owner_id=discovery.owner,
            agent_id=EFFECT_ATTEMPT_NAMESPACE, credential_key=row.credential_key,
            encrypted_value=mesh_enrollment._encode_record(value), updated_at=row.updated_at)
    response = await inventory(discovery)
    assert response.status_code == 200 and response.json()["attempts"][0]["attempt_nonce"] == row.credential_key
    remote_machines.delete_machine(discovery.source, discovery.owner, discovery.machine)
    response = await inventory(discovery)
    assert response.status_code == 409 and response.json()["detail"] == "reconcile_target_unavailable"


async def test_owned_operation_without_effects_returns_empty_inventory(discovery):
    accepted, _ = _submit_and_claim(discovery.node, uuid.uuid4().hex, discovery.owner)
    response = await discovery.client.get(f"/api/async-tasks/{accepted.operation_id}/effect-attempts",
                                          headers=discovery.headers)
    assert response.status_code == 200 and response.json()["attempts"] == []
    assert response.json()["operation_state"] == "running"


@pytest.mark.parametrize("field,value", [
    ("argument_digest", "d" * 64), ("target_id", "another-target"), ("target_kind", "mesh_member"),
])
async def test_discovered_identity_cannot_authorize_a_changed_report(discovery, field, value):
    attempt = (await inventory(discovery)).json()["attempts"][0]
    body = report_body(attempt, "confirmed_not_applied")
    body[field] = value
    response = await discovery.client.post(discovery.path + "/effect-reconcile", headers=discovery.headers, json=body)
    assert response.status_code == 409 and response.json()["detail"] == "reconcile_report_stale"
    current = (await inventory(discovery)).json()["attempts"][0]
    assert current == attempt and len(discovery.ssh.commands) == 1


async def test_target_retired_after_discovery_cannot_accept_a_report(discovery):
    attempt = (await inventory(discovery)).json()["attempts"][0]
    discovery.credentials.delete_machine_credential(discovery.machine, discovery.owner)
    response = await discovery.client.post(discovery.path + "/effect-reconcile", headers=discovery.headers,
        json=report_body(attempt, "confirmed_not_applied"))
    assert response.status_code == 409 and response.json()["detail"] == "reconcile_target_unavailable"
    assert len(discovery.ssh.commands) == 1


@pytest.mark.parametrize("kind", ["absent", "runtime_rebound"])
async def test_discovery_service_refuses_unbound_caller(discovery, human, bound, fixture, kind):
    from dataclasses import replace
    from orchestrator.work_reconciliation import ReconciliationRefused
    from tests.test_human_request_authority_088 import selected
    caller = object()
    if kind == "runtime_rebound":
        caller = await selected(human, bound, fixture, method="GET")
        caller = replace(caller, _binding=replace(caller._binding, runtime=object()))
    with pytest.raises(ReconciliationRefused, match="effect_current_authority_unavailable"):
        await discovery.orch.effect_reconciliation.list_current(caller,
            discovery.operation_id, orchestrator=discovery.orch)


async def test_session_revoked_during_discovery_withholds_identity(discovery, fixture, runtime, monkeypatch):
    original = discovery.ledger._list_in_transaction
    def revoke(tx, **kwargs):
        values = original(tx, **kwargs)
        row = runtime.repositories.history.sessions.get(tx, owner_id=fixture[1], session_id=fixture[2])
        runtime.repositories.history.sessions.delete_and_return(tx, owner_id=fixture[1],
            session_id=fixture[2], expected_incarnation_id=row.incarnation_id)
        return values
    monkeypatch.setattr(discovery.ledger, "_list_in_transaction", revoke)
    response = await inventory(discovery)
    assert response.status_code == 401 and "attempt_nonce" not in response.text
