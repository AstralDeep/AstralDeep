"""Exercises retained-result MCP resources against real Plane actions and result MACs.
Provider and public-page inputs are deterministic local seams; no external model or client is contacted.
"""

import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

from audit.repository import AuditRepository
from orchestrator.framework_credentials import FrameworkCredentialService
from orchestrator.mcp_authz import MCPAuthError
from orchestrator.mcp_server_endpoint import read_resource
from orchestrator.mcp_work_resources import WorkResourceAuthority
from orchestrator.work_operations import FrameworkWorkOperations
from personalization.phi_gate import PHIGate
from tests.helpers.session_consent_088 import consent_from_store
from tests.test_mcp_endpoint_064 import _Orchestrator
from tests.test_mcp_work_resources_postgres import _claims, _http
from tests.test_work_result_postgres_088 import (
    _mutate_action,
    _mutate_record,
    completed as completed,
    fixture as fixture,
    gate_orchestrator as gate_orchestrator,
    operation as operation,
    plane as plane,
    research as research,
    signing_key as signing_key,
)

runtime = plane
pytestmark = [pytest.mark.asyncio,
              pytest.mark.parametrize("operation", [{"tokens": 300_000}], indirect=True)]


@pytest_asyncio.fixture
async def result_owner(completed, monkeypatch):
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "framework_credentials", True)
    op = completed
    credentials = FrameworkCredentialService(
        plane_runtime=op.runtime, audit=AuditRepository(plane_runtime=op.runtime),
    )
    _, token = credentials.issue(
        owner_id=op.owner, caller=consent_from_store(op.sessions, op.owner, op.sid).observation,
        name="Synthetic retained-result reader", scopes=("artifacts.read",),
        expires_in_seconds=3600, max_admissions=10,
    )
    caller = credentials.resolve_bearer(token)
    assert caller is not None
    orch = _Orchestrator()
    orch.framework_credentials = credentials
    orch.framework_work_operations = FrameworkWorkOperations(
        assignments=op.executor.service, credentials=credentials,
    )
    orch.persistent_assignments = op.executor.service
    orch.audit_repo = AuditRepository(plane_runtime=op.runtime)
    return op, orch, caller, token


async def test_retained_result_http_uses_real_mac_with_artifacts_only_scope(result_owner):
    op, orch, caller, token = result_owner
    uri = f"astral://work/{op.completed.assignment_id}/result"
    response = await _http(orch, token, "resources/read", uri=uri)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    contents = response.json()["result"]["contents"]
    assert len(contents) == 1 and contents[0]["uri"] == uri
    view = json.loads(contents[0]["text"])
    assert view["id"] == op.completed.assignment_id
    assert view["revision"] == op.completed.state_version
    assert view["result"]["available"] is True
    assert view["result"]["content"]["passages"] == [{"id": "p001", "text": "Public release 088"}]
    assert "requested_url" not in contents[0]["text"] and "final_url" not in contents[0]["text"]
    assert token not in response.text and caller.credential_id not in response.text
    assert len(op.seed_observations["model_calls"]) == 1 and op.model_calls == []
    repeated = await read_resource(orch, caller.owner_id, uri, _claims(caller))
    assert repeated["boundedViewModel"] == view
    assert len(op.seed_observations["model_calls"]) == 1 and op.model_calls == []
    authority = WorkResourceAuthority(orch, caller.owner_id, _claims(caller), scope="artifacts.read")
    with pytest.raises(MCPAuthError):
        await authority.list()
    with pytest.raises(MCPAuthError):
        await authority.read(uri.replace("/result", "/status"))


@pytest.mark.parametrize("scopes,method,kind,expected", [
    (("artifacts.read",), "resources/read", "status", 403),
    (("artifacts.read",), "resources/list", "status", 403),
    (("operations.read",), "resources/read", "result", 403),
    (("operations.read", "artifacts.read"), "resources/read", "result", 200),
])
async def test_method_specific_scope_is_required(result_owner, scopes, method, kind, expected):
    op, orch, _, _ = result_owner
    _, token = orch.framework_credentials.issue(
        owner_id=op.owner, caller=consent_from_store(op.sessions, op.owner, op.sid).observation,
        name="Synthetic scoped reader", scopes=scopes, expires_in_seconds=3600, max_admissions=10,
    )
    response = await _http(orch, token, method, uri=f"astral://work/{op.completed.assignment_id}/{kind}")
    assert response.status_code == expected, response.text
    if expected != 200:
        assert "contents" not in response.text and "Public release" not in response.text


async def test_tampered_result_mac_keeps_truthful_unavailable_outcome(result_owner):
    op, orch, _, token = result_owner
    await _mutate_action(op, op.model, lambda data: data["result"].update(result_digest="0" * 64))
    response = await _http(orch, token, "resources/read", uri=f"astral://work/{op.completed.assignment_id}/result")
    assert response.status_code == 200, response.text
    view = json.loads(response.json()["result"]["contents"][0]["text"])
    assert view["result"] == {"version": 1, "available": False, "reason": "unavailable", "content": None}
    assert "Public release" not in response.text


@pytest.mark.parametrize("change,reason", [
    ("future_operation", "unsupported"), ("future_checkpoint", "unsupported"),
    ("nonretained", "not_retained"),
])
async def test_existing_unavailable_dispositions_survive_http(result_owner, change, reason):
    op, orch, _, token = result_owner

    def mutate(data):
        if change == "future_operation":
            data["operation"]["version"] = 3
        elif change == "future_checkpoint":
            data["checkpoint"]["schema_version"] = 2
        else:
            data["operation"]["source_retention"] = "none"

    await _mutate_record(op, mutate)
    response = await _http(orch, token, "resources/read", uri=f"astral://work/{op.completed.assignment_id}/result")
    assert response.status_code == 200, response.text
    view = json.loads(response.json()["result"]["contents"][0]["text"])
    assert view["result"] == {"version": 1, "available": False, "reason": reason, "content": None}
    assert "Public release" not in response.text


@pytest.mark.parametrize("stage", ["sharing", "admission"])
async def test_current_artifact_scope_is_rechecked_after_last_await(result_owner, monkeypatch, stage):
    op, orch, caller, token = result_owner
    credentials = orch.framework_credentials
    original = credentials.assert_execution
    attenuated = []

    def current(tx, observation):
        value = original(tx, observation)
        if attenuated:
            return replace(value, credential=replace(value.credential, scopes=("operations.read",)))
        return value

    monkeypatch.setattr(credentials, "assert_execution", current)
    if stage == "sharing":
        def screen(**_):
            attenuated.append(True)
            return []
        orch.persistent_assignments.phi_gate = PHIGate(analyzer=SimpleNamespace(analyze=screen))
    else:
        original_admission = orch._call_work_admission

        async def cleanup(method, *args, **kwargs):
            value = await original_admission(method, *args, **kwargs)
            if method == orch.work_admission.terminalize:
                attenuated.append(True)
            return value

        orch._call_work_admission = cleanup
    response = await _http(orch, token, "resources/read", uri=f"astral://work/{op.completed.assignment_id}/result")
    assert response.status_code in {403, 409}, response.text
    assert attenuated == [True]
    assert "Public release" not in response.text and "contents" not in response.text
    assert caller.has_scope("artifacts.read")


@pytest.mark.parametrize("stage", ["before", "sharing", "admission"])
async def test_revoke_result_credential_blocks_every_delivery(result_owner, stage):
    op, orch, caller, token = result_owner

    def revoke():
        orch.framework_credentials.revoke(owner_id=op.owner, credential_id=caller.credential_id)

    if stage == "before":
        revoke()
    elif stage == "sharing":
        def screen(**_):
            revoke()
            return []
        orch.persistent_assignments.phi_gate = PHIGate(analyzer=SimpleNamespace(analyze=screen))
    else:
        original = orch._call_work_admission

        async def cleanup(method, *args, **kwargs):
            value = await original(method, *args, **kwargs)
            if method == orch.work_admission.terminalize:
                revoke()
            return value

        orch._call_work_admission = cleanup
    response = await _http(orch, token, "resources/read", uri=f"astral://work/{op.completed.assignment_id}/result")
    assert response.status_code in {401, 403, 409}, response.text
    assert "Public release" not in response.text and "contents" not in response.text


async def test_foreign_owner_and_missing_result_never_expose_content(result_owner):
    op, orch, caller, _ = result_owner
    for claims in (_claims(replace(caller, owner_id=str(uuid4()))), _claims(caller)):
        identity = op.completed.assignment_id if claims["sub"] != op.owner else str(uuid4())
        with pytest.raises(MCPAuthError):
            await read_resource(orch, claims["sub"], f"astral://work/{identity}/result", claims)


async def test_actual_other_owner_bearer_cannot_read_retained_result(result_owner):
    op, orch, _, _ = result_owner
    owner, sid = str(uuid4()), uuid4().hex
    op.sessions.create(sid, user_id=owner, access_token="synthetic-access",
                       refresh_token="synthetic-refresh", hard_max_seconds=3600)
    _, token = orch.framework_credentials.issue(
        owner_id=owner, caller=consent_from_store(op.sessions, owner, sid).observation,
        name="Synthetic other owner", scopes=("artifacts.read",),
        expires_in_seconds=3600, max_admissions=10,
    )
    response = await _http(orch, token, "resources/read", uri=f"astral://work/{op.completed.assignment_id}/result")
    assert response.status_code == 404, response.text
    assert "Public release" not in response.text and "contents" not in response.text


@pytest.mark.parametrize("mode", ["clinical", "failure", "missing"])
async def test_local_result_sharing_policy_denies_content(result_owner, mode):
    op, orch, caller, _ = result_owner
    seen = []

    def screen(**kwargs):
        seen.append(kwargs["text"])
        if mode == "failure":
            raise RuntimeError("private-policy-error")
        return [SimpleNamespace(start=0, end=1, entity_type="MRN", score=1)]

    orch.persistent_assignments.phi_gate = None if mode == "missing" else PHIGate(analyzer=SimpleNamespace(analyze=screen))
    with pytest.raises(MCPAuthError):
        await read_resource(orch, caller.owner_id, f"astral://work/{op.completed.assignment_id}/result", _claims(caller))
    if mode != "missing":
        assert len(seen) == 1 and "Public release 088" in seen[0]
