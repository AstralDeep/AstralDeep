"""Tests current framework authority before replaying Work control receipts.
Actual MCP authorization and dispatch preserve revocation after the initial bearer check.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator.mcp_authz import authorize_mcp_request
from orchestrator.mcp_server_endpoint import _dispatch_work_tool
from persistent_agents.models import AssignmentError
from tests.helpers.plane_template import plane_template as plane_template
from tests.helpers.session_consent_088 import consent_from_store
from tests.test_work_subscriptions_postgres_088 import (
    _operation,
    _read,
    _env as _env,
    audit as audit,
    credentials as credentials,
    fixture as fixture,
    plane as plane,
    signing_key as signing_key,
    work as work,
)

runtime = plane


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["pause", "cancel"])
@pytest.mark.parametrize("boundary", ["stale_service_caller", "mcp_authorized_then_revoked"])
async def test_revoked_framework_caller_cannot_read_a_control_receipt(work, command, boundary):
    _, token = work.credentials.issue(
        owner_id=work.owner,
        caller=consent_from_store(work.store, work.owner, work.sid).observation,
        name="independent synthetic replay boundary",
        scopes=("operations.control", "operations.read"),
        expires_in_seconds=3600,
        max_admissions=10,
    )
    caller = work.credentials.resolve_bearer(token)
    target = _operation(work)
    arguments = {"operation_id": target.assignment_id, "submission_id": str(uuid4()), "expected_revision": target.state_version}
    first = await getattr(work.ops, command)(caller, **arguments)
    assert first["applied"] is True
    before = (await _read(work, target)).assignment
    replay = await getattr(work.ops, command)(caller, **arguments)
    assert replay["applied"] is False
    assert (await _read(work, target)).assignment == before
    if boundary == "mcp_authorized_then_revoked":
        claims = await authorize_mcp_request(
            headers={"authorization": "Bearer " + token},
            query_params={}, cookies={}, required_scopes=("mcp:tools:invoke",),
            resolve_framework_bearer=work.credentials.resolve_bearer,
        )
        fresh_response = await _dispatch_work_tool(
            SimpleNamespace(framework_work_operations=work.ops), claims,
            "astral_" + command + "_operation", arguments,
        )
        assert fresh_response.error is None and fresh_response.result["applied"] is False
        assert (await _read(work, target)).assignment == before
    work.credentials.revoke(owner_id=work.owner, credential_id=caller.credential_id)
    if boundary == "stale_service_caller":
        with pytest.raises(AssignmentError, match="framework_credential_authority_unavailable"):
            await getattr(work.ops, command)(caller, **arguments)
    else:
        response = await _dispatch_work_tool(
            SimpleNamespace(framework_work_operations=work.ops), claims,
            "astral_" + command + "_operation", arguments,
        )
        assert response.error == {"message": "framework_credential_authority_unavailable"}, response
    assert (await _read(work, target)).assignment == before
