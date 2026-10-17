"""Checks terminal delivery retains the original signed-cookie session and expiry.
Uses normal JWT/session authentication with an isolated AstralPlane runtime.
"""

import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI, Request

from orchestrator import session_store, terminal_api, terminal_security, web_auth
from shared.feature_flags import flags
from shared.protocol import MCPResponse
from tests.helpers.session_plane_runtime import (
    get_session_record, replace_session_record, web_session_store,
)
from tests.test_request_session_authority_088 import (
    fixture as fixture, runtime as runtime, signing_key as signing_key,
)

PAYLOAD = {
    "session_id": "retained-terminal", "offset": 0, "next_offset": 12,
    "data_b64": "T1dORVIgT1VUUFVU", "bytes": 12, "truncated": False,
    "eof": False, "session": {"session_id": "retained-terminal"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("transition,status", [
    ("unchanged", 200), ("rotated", 200),
    ("deleted", 401), ("same_sid_replaced", 401),
    ("deleted_permission", 401), ("replaced_permission", 401),
    ("store_replaced", 401), ("store_replaced_permission", 401),
    ("original_expired", 401), ("hard_expired_permission", 401),
    ("session_read_failure", 503), ("missing_selection", 401),
    ("malformed_selection", 401),
])
async def test_terminal_output_keeps_original_cookie_authority_across_waits(
    fixture, runtime, monkeypatch, transition, status,
):
    store, owner, sid, token, refreshes = fixture
    monkeypatch.setitem(flags._flags, "terminal_shell", True)
    initial = get_session_record(runtime, sid)
    now = time.time()
    clock = {"now": now}
    monkeypatch.setattr(terminal_security, "time", SimpleNamespace(time=lambda: clock["now"]))
    effects = []
    permissions = []
    app = FastAPI()

    def change_state():
        if transition in {"deleted", "deleted_permission"}:
            store.delete(sid)
        elif transition in {"same_sid_replaced", "replaced_permission"}:
            new = replace_session_record(runtime, initial)
            assert new.incarnation_id != initial.incarnation_id
        elif transition in {"store_replaced", "store_replaced_permission"}:
            app.state.orchestrator.web_sessions = web_session_store(runtime)
        elif transition in {"rotated", "original_expired"}:
            store.update_tokens(sid, access_token=token(exp=int(now) + 3600),
                                refresh_token="synthetic-rotated-refresh")
            if transition == "original_expired":
                clock["now"] = now + 600
        elif transition == "session_read_failure":
            def unavailable(*args, **kwargs):
                raise RuntimeError("private session repository failure")

            monkeypatch.setattr(store, "is_current_incarnation", unavailable)
        elif transition == "hard_expired_permission":
            monkeypatch.setattr(session_store, "time", SimpleNamespace(
                time=lambda: initial.hard_expires_at + 1,
            ))

    def allowed(*args):
        permissions.append(True)
        if transition.endswith("_permission"):
            change_state()
        return True

    async def dispatch(**kwargs):
        assert kwargs["claims"]["sub"] == owner
        assert kwargs["tool_name"] == "terminal_read" and kwargs["channel"] == "rest"
        effects.append("read_retained_output")
        if not transition.endswith("_permission"):
            change_state()
        return MCPResponse(result=dict(PAYLOAD))

    app.state.orchestrator = SimpleNamespace(
        execute_authorized_tool=dispatch, web_sessions=store,
        tool_permissions=SimpleNamespace(is_tool_allowed=allowed), security_flags={},
    )
    if transition in {"missing_selection", "malformed_selection"}:
        async def malformed_handoff(request: Request, credentials=Depends(terminal_api.security)):
            claims = await terminal_api.get_web_or_bearer_user_payload(request, credentials)
            request.state._authenticated_cookie_session = (
                None if transition == "missing_selection" else (sid, None)
            )
            return claims

        app.dependency_overrides[terminal_api.get_web_or_bearer_user_payload] = malformed_handoff
    app.include_router(terminal_api.terminal_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://request-authority.invalid",
    ) as client:
        response = await client.get("/api/terminals/retained-terminal/output", headers={
            "Cookie": "astral_session=" + web_auth._sign(sid),
        })
    assert effects == ["read_retained_output"] and refreshes == []
    assert response.status_code == status
    if status == 200:
        assert response.json() == PAYLOAD
    else:
        assert "data_b64" not in response.text and PAYLOAD["data_b64"] not in response.text
        assert "private session repository failure" not in response.text
    if transition.endswith("_permission"):
        assert permissions
