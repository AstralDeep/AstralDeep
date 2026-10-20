"""Checks final terminal REST delivery against current PostgreSQL-backed tool permission.
Revocation during final IAM lookup must suppress retained output after the actuator returned.
"""

import asyncio

import httpx
import pytest

from orchestrator.tool_permissions import ToolPermissionManager, turn_permission_memo
from shared.feature_flags import flags
from shared.protocol import MCPResponse
from tests.helpers.voice_plane_runtime import isolated_plane_runtime
from tests.test_terminal_security_boundaries import OWNER, PAYLOAD, SESSION, _claims, _client


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["allowed", "permission", "security"])
async def test_final_iam_await_rechecks_current_terminal_permission_before_output(monkeypatch, transition):
    monkeypatch.setitem(flags._flags, "terminal_shell", True)
    with isolated_plane_runtime("ad_terminal_delivery") as runtime:
        permissions = ToolPermissionManager(plane_runtime=runtime, plane_repositories=runtime.repositories)
        permissions.register_tool_scopes("terminal-shell-1", {"terminal_read": "tools:read"})
        permissions.set_tool_permission(OWNER, "terminal-shell-1", "terminal_read", "tools:read", True)
        final_auth = asyncio.Event()
        release = asyncio.Event()
        identities = 0

        async def identity():
            nonlocal identities
            identities += 1
            if identities == 2:
                final_auth.set()
                await release.wait()
            return _claims()

        async def dispatch(**kwargs):
            assert permissions.is_tool_allowed(OWNER, "terminal-shell-1", "terminal_read") is True
            return MCPResponse(result=dict(PAYLOAD))

        client, orch, _ = _client(monkeypatch, resolver=identity, dispatch=dispatch)
        orch.tool_permissions = permissions
        orch.security_flags = {}

        async def change_authority():
            await final_auth.wait()
            if transition == "permission":
                permissions.set_tool_permission(OWNER, "terminal-shell-1", "terminal_read", "tools:read", False)
            elif transition == "security":
                orch.security_flags["terminal-shell-1"] = {"terminal_read": {"blocked": True}}
            release.set()

        transport = httpx.ASGITransport(app=client.app)
        with turn_permission_memo():
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
                response, _ = await asyncio.wait_for(asyncio.gather(
                    api.get(f"/api/terminals/{SESSION}/output"), change_authority()), timeout=10)
        assert identities == 2
        if transition == "allowed":
            assert response.status_code == 200 and response.json() == PAYLOAD
        else:
            expected = "terminal_permission_denied" if transition == "permission" else "terminal_security_blocked"
            assert response.status_code == 403 and response.json() == {"error": expected}
            assert "data_b64" not in response.text and PAYLOAD["data_b64"] not in response.text
