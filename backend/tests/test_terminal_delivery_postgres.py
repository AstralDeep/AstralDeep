"""Checks that a terminal tool read revalidates durable permissions before returning buffered output.
The terminal agent and the isolated Plane runtime provide the normal delivery and permission paths.
"""

import pytest

from orchestrator.tool_permissions import ToolPermissionManager, turn_permission_memo
from tests.helpers.voice_plane_runtime import isolated_plane_runtime
from tests.test_terminal_agent_tools import OWNER, SESSION, retained as retained


@pytest.mark.parametrize("transition", ["allowed", "permission", "security"])
def test_output_requires_current_permission_after_terminal_read(retained, transition):
    agent, tool = "terminal-shell-1", "terminal_read"
    with isolated_plane_runtime("ad_terminal_delivery") as runtime:
        permissions = ToolPermissionManager(plane_runtime=runtime, plane_repositories=runtime.repositories)
        permissions.register_tool_scopes(agent, {tool: "tools:read"})
        permissions.set_tool_permission(OWNER, agent, tool, "tools:read", True)
        retained.orch.tool_permissions = permissions

        def read(*args, **kwargs):
            if transition == "permission":
                permissions.set_tool_permission(OWNER, agent, tool, "tools:read", False)
            elif transition == "security":
                retained.orch.security_flags[agent] = {tool: {"blocked": True}}
            return retained.payload

        retained.manager.read.side_effect = read
        with turn_permission_memo():
            result = retained.invoke(tool, session_id=SESSION)
        retained.manager.read.assert_called_once()
        if transition == "allowed":
            assert result["_data"]["data_b64"] == retained.payload["data_b64"]
        else:
            expected = "terminal_permission_denied" if transition == "permission" else "terminal_security_blocked"
            assert result["_data"]["code"] == expected
            assert "data_b64" not in result["_data"]
            assert retained.payload["data_b64"] not in str(result)
