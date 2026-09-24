"""Tests for orchestrator/tool_permissions.py: scope enforcement, RFC 8693 token
attenuation and act-claim structure, immediate effect of scope/override changes, and
cross-user permission isolation.
"""

import base64
import json

import pytest


AGENT_ID = "test-nefarious-agent"
USER_ID = "researcher-001"


@pytest.fixture
def configured_perm_manager(perm_manager):
    tool_scope_map = {
        "read_user_profile": "tools:read",
        "read_system_logs": "tools:read",
        "write_user_notes": "tools:write",
        "update_user_settings": "tools:write",
        "exfiltrate_data": "tools:system",
    }
    perm_manager.register_tool_scopes(AGENT_ID, tool_scope_map)
    # Resets shared perms — tests would leak state otherwise
    for user in (USER_ID, "user-alpha", "user-beta"):
        perm_manager.remove_agent_permissions(user, AGENT_ID)
    return perm_manager


def _decode_mock_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    payload_b64 = parts[1]
    padding = 4 - len(payload_b64) % 4
    if padding != 4:
        payload_b64 += "=" * padding
    return json.loads(base64.urlsafe_b64decode(payload_b64))


class TestPermissionDelegation:
    def test_scope_enforcement_blocks_unauthorized(self, configured_perm_manager):
        pm = configured_perm_manager
        pm.set_agent_scopes(USER_ID, AGENT_ID, {"tools:read": True})

        assert pm.is_tool_allowed(USER_ID, AGENT_ID, "read_user_profile") is True
        assert pm.is_tool_allowed(USER_ID, AGENT_ID, "read_system_logs") is True
        assert pm.is_tool_allowed(USER_ID, AGENT_ID, "write_user_notes") is False
        assert pm.is_tool_allowed(USER_ID, AGENT_ID, "exfiltrate_data") is False

    def test_token_attenuation_scopes(self, configured_perm_manager, delegation_service):
        pm = configured_perm_manager
        pm.set_agent_scopes(USER_ID, AGENT_ID, {
            "tools:read": True,
            "tools:write": False,
            "tools:system": False,
        })

        enabled = pm.get_enabled_scope_names(USER_ID, AGENT_ID)
        allowed = pm.get_allowed_tools(
            USER_ID, AGENT_ID,
            ["read_user_profile", "read_system_logs", "write_user_notes", "exfiltrate_data"],
        )

        result = delegation_service._create_mock_delegation_token(
            agent_id=AGENT_ID,
            allowed_tools=allowed,
            user_id=USER_ID,
            enabled_scopes=enabled,
        )

        token = result["access_token"]
        payload = _decode_mock_jwt_payload(token)
        scope_str = payload["scope"]

        assert "tools:read" in scope_str
        assert "tools:write" not in scope_str
        assert "tools:system" not in scope_str
        assert "tool:read_user_profile" in scope_str
        assert "tool:exfiltrate_data" not in scope_str

    def test_token_act_claim_structure(self, delegation_service):
        result = delegation_service._create_mock_delegation_token(
            agent_id="my-agent-42",
            allowed_tools=["some_tool"],
            user_id="user-abc",
            enabled_scopes=["tools:read"],
        )

        payload = _decode_mock_jwt_payload(result["access_token"])
        assert "act" in payload, "Missing 'act' claim per RFC 8693 §4.1"
        assert "sub" in payload["act"]
        assert payload["act"]["sub"] == "agent:my-agent-42"

    def test_permission_change_immediate_effect(self, configured_perm_manager):
        pm = configured_perm_manager

        pm.set_agent_scopes(USER_ID, AGENT_ID, {"tools:write": False})
        assert pm.is_tool_allowed(USER_ID, AGENT_ID, "write_user_notes") is False

        pm.set_agent_scopes(USER_ID, AGENT_ID, {"tools:write": True})
        assert pm.is_tool_allowed(USER_ID, AGENT_ID, "write_user_notes") is True

        pm.set_agent_scopes(USER_ID, AGENT_ID, {"tools:write": False})
        assert pm.is_tool_allowed(USER_ID, AGENT_ID, "write_user_notes") is False

    def test_per_tool_override(self, configured_perm_manager):
        pm = configured_perm_manager

        pm.set_agent_scopes(USER_ID, AGENT_ID, {"tools:write": True})
        assert pm.is_tool_allowed(USER_ID, AGENT_ID, "write_user_notes") is True
        assert pm.is_tool_allowed(USER_ID, AGENT_ID, "update_user_settings") is True

        pm.set_tool_overrides(USER_ID, AGENT_ID, {"write_user_notes": False})

        assert pm.is_tool_allowed(USER_ID, AGENT_ID, "write_user_notes") is False
        assert pm.is_tool_allowed(USER_ID, AGENT_ID, "update_user_settings") is True

    def test_cross_user_isolation(self, configured_perm_manager):
        pm = configured_perm_manager
        user_a = "user-alpha"
        user_b = "user-beta"

        pm.set_agent_scopes(user_a, AGENT_ID, {"tools:read": True, "tools:write": True})
        pm.set_agent_scopes(user_b, AGENT_ID, {"tools:read": True, "tools:write": False})

        assert pm.is_tool_allowed(user_a, AGENT_ID, "write_user_notes") is True
        assert pm.is_tool_allowed(user_b, AGENT_ID, "write_user_notes") is False
        assert pm.is_tool_allowed(user_a, AGENT_ID, "read_user_profile") is True
        assert pm.is_tool_allowed(user_b, AGENT_ID, "read_user_profile") is True
