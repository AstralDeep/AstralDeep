"""Checks terminal REST forwards only the subject token recorded by verified authentication.
Real JWT and Plane cookie authentication feed both safe reads and the normal human approval handoff.
"""

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Depends, FastAPI, Request

from orchestrator import remote_confirmation as rc, terminal_api, web_auth
from orchestrator.auth import get_web_or_bearer_user_payload
from shared.protocol import MCPResponse
from tests import test_request_session_authority_088 as identity_tests
from tests import test_terminal_approvals as review_tests

identity = identity_tests.fixture
runtime = identity_tests.runtime
signing_key = identity_tests.signing_key
review = review_tests.fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["list_terminals", "start_terminal"])
@pytest.mark.parametrize("source", ["bearer", "cookie"])
@pytest.mark.parametrize("state", ["valid", "absent", "empty", "integer", "mapping"])
async def test_verified_subject_token_reaches_dispatch_without_claim_or_argument_fallback(
    identity, runtime, review, monkeypatch, tool, source, state,
):
    store, owner, sid, token, refreshes = identity
    expected = token() if source == "bearer" else store.get(sid)["access_token"]
    supplied = "untrusted-claim-token"
    app = FastAPI()
    app.state.orchestrator = review.orch
    review.orch.web_sessions = store
    review.orch.send_ui_render = AsyncMock(return_value=True)
    effects = []

    async def authenticated(request: Request, credentials=Depends(terminal_api.security)):
        claims = await get_web_or_bearer_user_payload(request, credentials)
        assert request.state.delegation_subject_token == expected
        claims["_raw_token"] = supplied
        review.claims.clear()
        review.claims.update(claims)
        if state == "absent":
            del request.state.delegation_subject_token
        elif state != "valid":
            request.state.delegation_subject_token = {"empty": "", "integer": 7, "mapping": {"token": supplied}}[state]
        return claims

    async def dispatched(**kwargs):
        delegated = kwargs.get("delegation_subject_token")
        assert delegated == (expected if state == "valid" else None)
        assert expected not in str(kwargs["arguments"]) and supplied not in str(kwargs["arguments"])
        if delegated is None:
            return MCPResponse(error={"code": "terminal_delegation_unavailable", "retryable": False})
        effects.append(kwargs["tool_name"])
        if tool == "list_terminals":
            return MCPResponse(result={"sessions": []})
        result = rc.evaluate(review.orch, None, kwargs["agent_id"], tool, kwargs["arguments"], None, owner)
        return MCPResponse(result={"_data": {"status": "confirmation_required"}}, ui_components=result[1])

    review.orch.execute_authorized_tool = dispatched
    app.dependency_overrides[get_web_or_bearer_user_payload] = authenticated
    app.include_router(terminal_api.terminal_router)
    monkeypatch.setattr(terminal_api, "require_terminal_execution", lambda *args: None)
    headers = ({"Authorization": "Bearer " + expected} if source == "bearer"
               else {"Cookie": "astral_session=" + web_auth._sign(sid)})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://request-authority.invalid") as client:
        if tool == "list_terminals":
            response = await client.get("/api/terminals", headers=headers)
        else:
            response = await client.post("/api/terminals", headers=headers, json={"cols": 80, "rows": 24})
    assert expected not in response.text and supplied not in response.text
    assert refreshes == []
    if state != "valid":
        assert response.status_code == 403 and response.json() == {"error": "terminal_delegation_unavailable"}
        assert not effects and review.db.rows == {}
    elif tool == "list_terminals":
        assert response.status_code == 200 and response.json() == {"sessions": []}
        assert effects == [tool]
    else:
        assert response.status_code == 202 and response.json()["status"] == "pending"
        assert effects == [tool]
        assert review.orch.send_ui_render.await_count == 1
        assert len(review.db.rows) == 1
        row = next(iter(review.db.rows.values()))
        assert expected not in str(row) and supplied not in str(row)
        assert review.orch.execute_single_tool.calls == []


@pytest.mark.asyncio
async def test_rejected_bearer_cannot_supply_delegation_or_create_proposal(identity, review):
    app = FastAPI()
    app.state.orchestrator = review.orch
    review.orch.execute_authorized_tool = AsyncMock()
    app.include_router(terminal_api.terminal_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://request-authority.invalid") as client:
        response = await client.get("/api/terminals", headers={"Authorization": "Bearer unverified-token"})
    assert response.status_code == 401
    assert "unverified-token" not in response.text
    review.orch.execute_authorized_tool.assert_not_awaited()
    assert review.db.rows == {}
