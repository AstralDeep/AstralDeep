"""Exercises MCP Work resources through real credentials, Plane reads, and HTTP admission.
Sharing and revocation controls use local synthetic data without a model or external client.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from audit.repository import AuditRepository
from orchestrator.mcp_authz import MCPAuthError, authorize_mcp_request
from orchestrator.mcp_server_endpoint import create_mcp_router, list_resources, read_resource
from personalization.phi_gate import PHIGate
from persistent_agents.models import AssignmentError
from tests.test_mcp_endpoint_064 import META, _Orchestrator, _request
from tests.test_work_framework_compat_088 import (
    _caller,
    _env as _env,
    _issue,
    assignments as assignments,
    credentials as credentials,
    ops as ops,
    runtime as runtime,
    session as session,
)


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def resource_owner(ops, credentials, session):
    store, owner, sid = session
    _, token = _issue(credentials, store, owner, sid)
    caller = _caller(credentials, token)
    record = await ops.submit(
        caller, idempotency_key=str(uuid.uuid4()), name="Synthetic work",
        instructions="Summarize the synthetic fixture.",
    )
    orch = _Orchestrator()
    orch.framework_work_operations = ops
    orch.framework_credentials = credentials
    orch.audit_repo = AuditRepository(plane_runtime=credentials.plane_runtime)
    orch.persistent_assignments = ops.assignments
    ops.assignments.orch = orch
    return orch, caller, token, record


def _claims(caller):
    return {"sub": caller.owner_id, "_framework_caller": caller}


def _uri(record, kind="status"):
    return f"astral://work/{record['id']}/{kind}"


async def _subscription(orch, caller, record, session):
    from tests.test_work_subscriptions_postgres_088 import _operation

    store, owner, sid = session
    fixture = SimpleNamespace(runtime=orch.framework_credentials.plane_runtime,
                              store=store, owner=owner, sid=sid)
    target = await asyncio.to_thread(_operation, fixture)
    await orch.framework_work_operations.subscribe(
        caller, source_operation_id=record["id"], target_operation_id=target.assignment_id,
        submission_id=str(uuid.uuid4()), expected_revision=target.state_version,
    )
    before = await orch.framework_work_operations.subscriptions(caller)
    assert len(before["subscriptions"]) == 1
    assert before["subscriptions"][0]["source_operation_id"] == record["id"]
    return target.assignment_id


async def _http(orch, token, method, *, uri=None):
    params = {"_meta": META}
    if uri is not None:
        params["uri"] = uri
    app = FastAPI()
    app.include_router(create_mcp_router(orch, public_base_url="http://mcp.test"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mcp.test") as client:
        return await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers={"Authorization": f"Bearer {token}",
                     "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method},
        )


async def _tool_http(orch, token, name, arguments):
    body, headers = _request("tools/call", {"name": name, "arguments": arguments},
                             token=token, headers={"Mcp-Name": name})
    app = FastAPI()
    app.include_router(create_mcp_router(orch, public_base_url="http://mcp.test"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mcp.test") as client:
        return await client.post("/mcp", json=body, headers=headers)


@pytest.mark.parametrize("method", ["resources/list", "resources/read"])
async def test_real_framework_bearer_reads_only_status(resource_owner, monkeypatch, method):
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "framework_credentials", True)
    orch, caller, token, record = resource_owner
    response = await _http(orch, token, method, uri=_uri(record))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    result = response.json()["result"]
    if method == "resources/list":
        assert [item["uri"] for item in result["resources"]] == [_uri(record)]
        assert result["resources"][0]["name"] == "Status: Synthetic work"
    else:
        view = json.loads(result["contents"][0]["text"])
        assert view["id"] == record["id"] and view["title"] == "Synthetic work"
        assert set(view) == {"id", "title", "kind", "disposition", "lifecycle", "phase",
                             "revision", "createdAt", "updatedAt"}
    assert token not in response.text and caller.credential_id not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["owner", "claims", "hash", "scope", "delegation"])
async def test_resource_rejects_attenuated_or_mismatched_authority(resource_owner, change):
    orch, caller, _, record = resource_owner
    claims = _claims(caller)
    user = caller.owner_id
    if change == "owner":
        user = str(uuid.uuid4())
    elif change == "claims":
        claims["sub"] = str(uuid.uuid4())
    elif change == "hash":
        claims["_framework_caller"] = replace(caller, _token_hash="0" * 64)
    elif change == "scope":
        claims["_framework_caller"] = replace(caller, scopes=frozenset({"artifacts.read"}))
    else:
        claims["act"] = {"sub": "delegated-agent"}
    with pytest.raises(MCPAuthError):
        await read_resource(orch, user, _uri(record), claims)


@pytest.mark.asyncio
async def test_cached_caller_revocation_blocks_listing_and_read(resource_owner):
    orch, caller, _, record = resource_owner
    orch.framework_credentials.revoke(owner_id=caller.owner_id, credential_id=caller.credential_id)
    for method in ("list", "read"):
        with pytest.raises(MCPAuthError):
            if method == "list":
                await list_resources(orch, caller.owner_id, _claims(caller))
            else:
                await read_resource(orch, caller.owner_id, _uri(record), _claims(caller))


@pytest.mark.asyncio
async def test_artifact_resource_is_unavailable(resource_owner):
    orch, caller, _, record = resource_owner
    with pytest.raises(MCPAuthError) as error:
        await read_resource(orch, caller.owner_id, _uri(record, "artifacts"), _claims(caller))
    assert error.value.status_code in {400, 404}


@pytest.mark.asyncio
async def test_phi_bearing_legacy_title_never_crosses_resource_boundary(resource_owner, monkeypatch):
    orch, caller, _, _ = resource_owner
    gate = orch.persistent_assignments.phi_gate
    with monkeypatch.context() as ingress:
        ingress.setattr(gate, "contains_phi", lambda _: False)
        legacy = await orch.framework_work_operations.submit(
            caller, idempotency_key=str(uuid.uuid4()), name="Patient MRN: 1234567",
            instructions="Synthetic retained legacy fixture.",
        )
    for method in ("list", "read"):
        with pytest.raises(MCPAuthError):
            if method == "list":
                await list_resources(orch, caller.owner_id, _claims(caller))
            else:
                await read_resource(orch, caller.owner_id, _uri(legacy), _claims(caller))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["absent", "unavailable", "failure"])
async def test_resource_sharing_policy_unavailability_fails_closed(resource_owner, mode):
    orch, caller, _, record = resource_owner
    if mode == "absent":
        gate = None
    elif mode == "unavailable":
        gate = PHIGate(build_if_missing=False)
    else:
        def fail(**_):
            raise RuntimeError("synthetic private analyzer error")
        gate = PHIGate(analyzer=SimpleNamespace(analyze=fail))
    orch.persistent_assignments.phi_gate = gate
    with pytest.raises(MCPAuthError):
        await read_resource(orch, caller.owner_id, _uri(record), _claims(caller))
    ordinary = await orch.framework_work_operations.get(caller, record["id"])
    assert ordinary["title"] == "Synthetic work"


@pytest.mark.asyncio
async def test_revoke_while_sharing_check_is_awaited_denies_delivery(resource_owner):
    orch, caller, _, record = resource_owner

    def revoke(**_):
        orch.framework_credentials.revoke(owner_id=caller.owner_id, credential_id=caller.credential_id)
        return []

    orch.persistent_assignments.phi_gate = PHIGate(analyzer=SimpleNamespace(analyze=revoke))
    with pytest.raises(MCPAuthError):
        await read_resource(orch, caller.owner_id, _uri(record), _claims(caller))


async def test_revoke_during_admission_release_denies_http_delivery(resource_owner, monkeypatch):
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "framework_credentials", True)
    orch, caller, token, record = resource_owner
    original = orch._call_work_admission
    released = []

    async def release(method, *args, **kwargs):
        result = await original(method, *args, **kwargs)
        if method == orch.work_admission.terminalize:
            released.append(True)
            orch.framework_credentials.revoke(
                owner_id=caller.owner_id, credential_id=caller.credential_id,
            )
        return result

    orch._call_work_admission = release
    response = await _http(orch, token, "resources/read", uri=_uri(record))
    assert response.status_code in {401, 403, 409}
    assert released == [True]
    assert "Synthetic work" not in response.text and "contents" not in response.text


async def test_authoritative_execution_scope_remains_required(resource_owner, monkeypatch):
    orch, caller, _, record = resource_owner
    original = orch.framework_credentials.assert_execution

    def attenuate(tx, observation):
        current = original(tx, observation)
        return replace(current, credential=replace(current.credential, scopes=("artifacts.read",)))

    monkeypatch.setattr(orch.framework_credentials, "assert_execution", attenuate)
    with pytest.raises(MCPAuthError):
        await read_resource(orch, caller.owner_id, _uri(record), _claims(caller))


async def test_foreign_work_is_not_visible_and_owner_listing_round_trips(resource_owner, session):
    orch, caller, _, record = resource_owner
    store, _, _ = session
    foreign_owner, sid = str(uuid.uuid4()), uuid.uuid4().hex
    store.create(sid, user_id=foreign_owner, access_token="synthetic-access",
                 refresh_token="synthetic-refresh", hard_max_seconds=3600)
    _, token = _issue(orch.framework_credentials, store, foreign_owner, sid)
    foreign_caller = _caller(orch.framework_credentials, token)
    assert await list_resources(orch, foreign_owner, _claims(foreign_caller)) == []
    foreign = await orch.framework_work_operations.submit(
        foreign_caller, idempotency_key=str(uuid.uuid4()), name="Foreign work",
        instructions="Synthetic other owner.",
    )
    with pytest.raises(MCPAuthError) as error:
        await read_resource(orch, caller.owner_id, _uri(foreign), _claims(caller))
    assert error.value.status_code == 404
    resources = await list_resources(orch, caller.owner_id, _claims(caller))
    assert [item["uri"] for item in resources] == [_uri(record)]
    for item in resources:
        view = await read_resource(orch, caller.owner_id, item["uri"], _claims(caller))
        assert view["boundedViewModel"]["id"] == record["id"]


@pytest.mark.parametrize("scopes, permitted", [(("operations.read",), True),
                                               (("artifacts.read",), False)])
async def test_http_transport_scope_never_replaces_work_permission(resource_owner, session, monkeypatch,
                                                                scopes, permitted):
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "framework_credentials", True)
    orch, _, _, record = resource_owner
    store, owner, sid = session
    _, token = _issue(orch.framework_credentials, store, owner, sid, scopes=scopes)
    response = await _http(orch, token, "resources/read", uri=_uri(record))
    assert response.status_code == (200 if permitted else 403)
    if not permitted:
        assert "Synthetic work" not in response.text


@pytest.mark.parametrize("mode", ["revoke", "expired_observation", "no_observation"])
async def test_read_transaction_rejects_stale_authority(resource_owner, monkeypatch, mode):
    from datetime import timedelta

    orch, caller, _, record = resource_owner
    credentials = orch.framework_credentials
    original = credentials.fresh_observation

    def stale(value):
        observation = original(value)
        if mode == "revoke":
            credentials.revoke(owner_id=caller.owner_id, credential_id=caller.credential_id)
        elif mode == "expired_observation":
            return replace(observation, started_at=observation.started_at - timedelta(minutes=1),
                           valid_until=observation.valid_until - timedelta(minutes=1))
        else:
            return None
        return observation

    monkeypatch.setattr(credentials, "fresh_observation", stale)
    with pytest.raises(MCPAuthError):
        await read_resource(orch, caller.owner_id, _uri(record), _claims(caller))


@pytest.mark.parametrize("operation", ["read", "list"])
async def test_repository_failure_returns_no_resource_data(resource_owner, monkeypatch, operation):
    orch, caller, _, record = resource_owner

    async def fail(*_, **__):
        raise AssignmentError("private_repository_failure", 503)

    monkeypatch.setattr(orch.persistent_assignments.store, "transaction", fail)
    with pytest.raises(MCPAuthError) as error:
        if operation == "read":
            await read_resource(orch, caller.owner_id, _uri(record), _claims(caller))
        else:
            await list_resources(orch, caller.owner_id, _claims(caller))
    assert error.value.status_code == 503
    assert "private_repository_failure" not in str(error.value)


@pytest.mark.parametrize("change", ["credentials", "store", "repository", "gate"])
async def test_rebound_service_identity_is_not_authority(resource_owner, monkeypatch, change):
    orch, caller, _, record = resource_owner
    if change == "credentials":
        monkeypatch.setattr(orch.framework_work_operations, "credentials", object())
    elif change == "store":
        monkeypatch.setattr(orch.framework_work_operations, "store", object())
    elif change == "repository":
        monkeypatch.setattr(orch.persistent_assignments.store, "repository", object())
    else:
        monkeypatch.setattr(orch.persistent_assignments, "phi_gate", object())
    with pytest.raises(MCPAuthError):
        await read_resource(orch, caller.owner_id, _uri(record), _claims(caller))


async def test_revoked_token_is_rejected_by_real_http_auth(resource_owner, monkeypatch):
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "framework_credentials", True)
    orch, caller, token, record = resource_owner
    orch.framework_credentials.revoke(owner_id=caller.owner_id, credential_id=caller.credential_id)
    response = await _http(orch, token, "resources/read", uri=_uri(record))
    assert response.status_code == 401
    assert "Synthetic work" not in response.text


async def test_actual_resolved_authority_cannot_be_reused_after_revoke(resource_owner):
    orch, caller, token, record = resource_owner
    claims = await authorize_mcp_request(
        headers={"authorization": f"Bearer {token}"}, query_params={}, cookies={},
        required_scopes=("mcp:tools:read",),
        resolve_framework_bearer=orch.framework_credentials.resolve_bearer,
    )
    orch.framework_credentials.revoke(owner_id=caller.owner_id, credential_id=caller.credential_id)
    with pytest.raises(MCPAuthError):
        await read_resource(orch, caller.owner_id, _uri(record), claims)


@pytest.mark.parametrize("kind", ["artifacts", "missing", "malformed"])
async def test_unsupported_resource_http_returns_no_work_content(resource_owner, monkeypatch, kind):
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "framework_credentials", True)
    orch, _, token, record = resource_owner
    uri = None if kind == "missing" else _uri(record, kind)
    response = await _http(orch, token, "resources/read", uri=uri)
    assert response.status_code == 400
    assert "result" not in response.json()
    assert "Synthetic work" not in response.text


@pytest.mark.parametrize("name", ["astral_get_operation", "astral_list_operations",
                                  "astral_get_operation_events", "astral_get_artifact",
                                  "astral_emergency_status",
                                  "astral_list_operation_completion_subscriptions"])
async def test_framework_tool_read_revocation_during_release_denies_delivery(resource_owner,
                                                                           monkeypatch, name,
                                                                           session):
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "framework_credentials", True)
    orch, caller, token, record = resource_owner
    if name == "astral_emergency_status":
        from orchestrator.emergency_stop import EmergencyStopCoordinator

        orch.emergency_stop = EmergencyStopCoordinator()
    elif name == "astral_list_operation_completion_subscriptions":
        await _subscription(orch, caller, record, session)
    original = orch._call_work_admission
    released = []

    async def release(method, *args, **kwargs):
        result = await original(method, *args, **kwargs)
        if method == orch.work_admission.terminalize:
            released.append(True)
            orch.framework_credentials.revoke(
                owner_id=caller.owner_id, credential_id=caller.credential_id,
            )
        return result

    orch._call_work_admission = release
    response = await _tool_http(orch, token, name, {"operation_id": record["id"]})
    assert released == [True] and response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"] == {}
    assert "Synthetic work" not in response.text and record["id"] not in response.text


async def test_subscription_scan_revocation_denies_direct_return(resource_owner, session, monkeypatch):
    orch, caller, _, record = resource_owner
    await _subscription(orch, caller, record, session)
    original = orch.framework_work_operations.store.transaction
    observed = []

    async def scan(*args, **kwargs):
        result = await original(*args, **kwargs)
        if not observed:
            observed.append(True)
            await asyncio.to_thread(orch.framework_credentials.revoke,
                                    owner_id=caller.owner_id, credential_id=caller.credential_id)
        return result

    monkeypatch.setattr(orch.framework_work_operations.store, "transaction", scan)
    with pytest.raises(AssignmentError):
        await orch.framework_work_operations.subscriptions(caller)
    assert observed == [True]


@pytest.mark.parametrize("name", ["astral_emergency_status",
                                  "astral_list_operation_completion_subscriptions"])
async def test_current_status_and_subscription_http_reads_succeed(resource_owner, session,
                                                                 monkeypatch, name):
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "framework_credentials", True)
    orch, caller, token, record = resource_owner
    target = None
    if name == "astral_emergency_status":
        from orchestrator.emergency_stop import EmergencyStopCoordinator

        orch.emergency_stop = EmergencyStopCoordinator()
        await orch.emergency_stop.engage(caller.owner_id, reason="Synthetic owner stop")
    else:
        target = await _subscription(orch, caller, record, session)
    response = await _tool_http(orch, token, name, {})
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is False
    content = result["structuredContent"]
    if target is None:
        assert content["state"] == "stopped" and content["reason"] == "Synthetic owner stop"
    else:
        assert len(content["subscriptions"]) == 1
        assert content["subscriptions"][0]["source_operation_id"] == record["id"]
        assert content["subscriptions"][0]["target_operation_id"] == target
    assert token not in response.text


@pytest.mark.asyncio
async def test_panel_resource_serves_bounded_view_with_meta(resource_owner, monkeypatch):
    from orchestrator.mcp_panel import panel_state
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "framework_credentials", True)
    monkeypatch.setitem(flags._flags, "mcp_server", True)
    monkeypatch.setitem(flags._flags, "mcp_work_panel", True)
    orch, caller, token, record = resource_owner
    response = await _http(orch, token, "resources/read", uri=_uri(record, "panel"))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    result = response.json()["result"]
    assert result["_meta"]["astral/panelKind"] == "astral_work_status"
    assert result["_meta"]["astral/dataFlow"] == "owner-scoped; no third-party data flow"
    view = json.loads(result["contents"][0]["text"])
    current = await orch.framework_work_operations.get(caller, record["id"])
    expected = {
        "kind": "astral_work_status", "state": panel_state(current["disposition"]),
        "title": "Synthetic work",
    }
    if current["phase"] is not None:
        expected["detail"] = current["phase"]
    assert view == expected
    assert token not in response.text and caller.credential_id not in response.text
    assert "owner" not in view and "http" not in json.dumps(view)


@pytest.mark.asyncio
async def test_panel_resource_is_unavailable_while_disabled(resource_owner, monkeypatch):
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "mcp_server", True)
    monkeypatch.setitem(flags._flags, "mcp_work_panel", False)
    orch, caller, _, record = resource_owner
    with pytest.raises(MCPAuthError) as error:
        await read_resource(orch, caller.owner_id, _uri(record, "panel"), _claims(caller))
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_panel_resource_requires_the_status_scope(resource_owner):
    from dataclasses import replace

    orch, caller, _, record = resource_owner
    limited = replace(caller, scopes=frozenset({"artifacts.read"}))
    with pytest.raises(MCPAuthError) as error:
        await read_resource(orch, caller.owner_id, _uri(record, "panel"), _claims(limited))
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_panel_descriptors_ride_the_owner_scoped_listing(resource_owner, monkeypatch):
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "framework_credentials", True)
    monkeypatch.setitem(flags._flags, "mcp_server", True)
    monkeypatch.setitem(flags._flags, "mcp_work_panel", True)
    orch, caller, token, record = resource_owner
    response = await _http(orch, token, "resources/list")
    assert response.status_code == 200, response.text
    resources = response.json()["result"]["resources"]
    assert [item["uri"] for item in resources] == [
        f"astral://work/{record['id']}/status", f"astral://work/{record['id']}/panel",
    ]
    descriptor = resources[1]
    assert descriptor["mimeType"] == "application/json"
    assert descriptor["_meta"]["astral/panelKind"] == "astral_work_status"
    assert token not in response.text


@pytest.mark.asyncio
async def test_revoked_credential_cannot_read_the_panel(resource_owner, monkeypatch):
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "mcp_server", True)
    monkeypatch.setitem(flags._flags, "mcp_work_panel", True)
    orch, caller, _, record = resource_owner
    orch.framework_credentials.revoke(owner_id=caller.owner_id, credential_id=caller.credential_id)
    with pytest.raises(MCPAuthError):
        await read_resource(orch, caller.owner_id, _uri(record, "panel"), _claims(caller))


@pytest.mark.asyncio
async def test_panel_uri_guessing_fails_closed(resource_owner, monkeypatch):
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "mcp_server", True)
    monkeypatch.setitem(flags._flags, "mcp_work_panel", True)
    orch, caller, _, _ = resource_owner
    stranger = "bef2c1aa-9b4d-4f7a-8c1e-2d5b6a7c8e9a"
    with pytest.raises(MCPAuthError) as error:
        await read_resource(orch, caller.owner_id, f"astral://work/{stranger}/panel",
                            _claims(caller))
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_phi_bearing_title_never_crosses_the_panel(resource_owner, monkeypatch):
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "mcp_server", True)
    monkeypatch.setitem(flags._flags, "mcp_work_panel", True)
    orch, caller, _, _ = resource_owner
    gate = orch.persistent_assignments.phi_gate
    with monkeypatch.context() as ingress:
        ingress.setattr(gate, "contains_phi", lambda _: False)
        legacy = await orch.framework_work_operations.submit(
            caller, idempotency_key=str(uuid.uuid4()), name="Patient MRN: 1234567",
            instructions="Synthetic panel legacy fixture.",
        )
    with pytest.raises(MCPAuthError):
        await read_resource(orch, caller.owner_id, _uri(legacy, "panel"), _claims(caller))
