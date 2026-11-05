"""Covers server-owned settings actions for editing connectors and authorizing OAuth."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from orchestrator import remote_mcp_connectors
from orchestrator.projection_surfaces import remote_mcp


class CredentialManager:
    def __init__(self):
        self.records = {}

    def get_credential(self, owner, agent, key):
        return self.records.get((owner, agent, key))

    def set_credential(self, owner, agent, key, value, e2e=False):
        self.records[(owner, agent, key)] = value

    def compare_and_set_credential(self, owner, agent, key, expected, value):
        identity = (owner, agent, key)
        if self.records.get(identity) != expected:
            return False
        self.records[identity] = value
        return True

    def list_credential_keys(self, owner, agent):
        return [key for current_owner, current_agent, key in self.records
                if (owner, agent) == (current_owner, current_agent)]

    def delete_credential(self, owner, agent, key):
        self.records.pop((owner, agent, key), None)


def _orch():
    return SimpleNamespace(credential_manager=CredentialManager())


def test_native_edit_form_submission_keeps_connector_id(monkeypatch):
    orch = _orch()
    monkeypatch.setattr(remote_mcp, "_enabled", lambda: True)
    saved = remote_mcp_connectors.save_connector(orch, "owner", {
        "name": "Old", "url": "https://mcp.example/mcp", "transport": "streamable_http",
        "auth_type": "none",
    })
    connector_id = saved["id"]

    edit = asyncio.run(remote_mcp._edit(orch, None, "owner", [], {"connector_id": connector_id}))
    result = asyncio.run(remote_mcp._save(orch, None, "owner", [], {
        "connector_id": edit[1]["edit"],
        "fields": {"name": "Updated", "url": "https://mcp.example/mcp",
                   "transport": "streamable_http", "auth_type": "none"},
    }))

    assert "Remote MCP server saved." in result[2]
    current = remote_mcp_connectors.get_connector(orch, "owner", connector_id)
    assert current["id"] == connector_id
    assert current["name"] == "Updated"


def test_oauth_authorization_link_is_visible_in_sdui_without_credentials(monkeypatch):
    orch = _orch()
    auth_url = "https://login.example/authorize?state=opaque&code_challenge=public"
    monkeypatch.setattr(remote_mcp, "_enabled", lambda: True)
    monkeypatch.setattr("orchestrator.mcp_authz.canonical_public_base_url", lambda: "https://deep.example")

    async def begin(_orch, owner, connector_id, redirect_uri):
        assert owner == "owner"
        assert connector_id == "a" * 32
        assert redirect_uri == "https://deep.example/api/remote-mcp/oauth/callback"
        return auth_url

    monkeypatch.setattr("orchestrator.remote_mcp_oauth.begin_authorization", begin)
    result = asyncio.run(remote_mcp._oauth_start(
        orch, None, "owner", [], {"connector_id": "a" * 32}))
    components = asyncio.run(remote_mcp.components(orch, "owner", [], result[1]))
    rendered = json.dumps(components)

    assert "[Open OAuth authorization page]" in rendered
    assert auth_url in rendered
    assert "client-secret-value" not in rendered


def test_surface_render_edit_and_cards_escape_remote_metadata(monkeypatch):
    orch = _orch()
    monkeypatch.setattr(remote_mcp, "_enabled", lambda: True)
    saved = remote_mcp_connectors.save_connector(orch, "owner", {
        "name": "<Workspace>", "url": "https://mcp.example/mcp", "transport": "sse",
        "auth_type": "oauth", "oauth_client_id": "client-id", "oauth_client_secret": "never-render",
        "tools_catalog": [{"name": "echo", "description": "<img src=x>", "inputSchema": {"type": "object"}}],
        "tools": {"echo": True},
    })
    html = asyncio.run(remote_mcp.render(orch, "owner", [], {"edit": saved["id"]}))
    assert "&lt;Workspace&gt;" in html
    assert "&lt;img src=x&gt;" in html
    assert "never-render" not in html
    assert f'value="{saved["id"]}"' in html
    assert 'name="oauth_client_id"' in html
    assert "Legacy SSE" in html
    assert 'name="clear_oauth_client_credentials"' in html
    assert "chrome_remote_mcp_toggle_tool" in html


def test_connector_cards_open_server_owned_security_notice(monkeypatch):
    orch = _orch()
    monkeypatch.setattr(remote_mcp, "_enabled", lambda: True)
    remote_mcp_connectors.save_connector(orch, "owner", {
        "name": "Example", "url": "https://mcp.example/mcp", "transport": "streamable_http",
        "auth_type": "none",
    })

    html = asyncio.run(remote_mcp.render(orch, "owner", [], {}))
    details = asyncio.run(remote_mcp.render(
        orch, "owner", [], {"notice": "external_mcp_security"}))
    components = asyncio.run(remote_mcp.components(orch, "owner", [], {}))
    notice_components = asyncio.run(remote_mcp.components(
        orch, "owner", [], {"notice": "external_mcp_security"}))
    serialized = json.dumps(notice_components)
    card = next(component for component in components if component["type"] == "card")
    header = card["content"][0]
    warning = header["children"][1]

    assert 'data-ui-action="chrome_open"' in html
    assert "external_mcp_security" in html
    assert "Standard MCP connection" in details
    assert "provider controls its data handling" in details
    assert card["title"] == ""
    assert header["children"][0]["content"] == "Example"
    assert warning["label"] == "⚠"
    assert warning["action"] == "chrome_open"
    assert warning["payload"] == {
        "surface": "remote_mcp", "params": {"notice": "external_mcp_security"},
    }
    assert warning["aria-label"] == "Standard MCP connection"
    assert warning["aria-description"] == remote_mcp._security_notice()
    assert warning["id"] == warning["data-testid"]
    assert "Standard MCP connection" in serialized
    assert "provider controls its data handling" in serialized


def test_native_form_labels_auth_fields_and_oauth_clear_visibility(monkeypatch):
    orch = _orch()
    monkeypatch.setattr(remote_mcp, "_enabled", lambda: True)
    none_connector = remote_mcp_connectors.save_connector(orch, "owner", {
        "name": "No auth", "url": "https://mcp.example/none", "transport": "sse",
        "auth_type": "none",
    })
    oauth_connector = remote_mcp_connectors.save_connector(orch, "owner", {
        "name": "OAuth", "url": "https://mcp.example/oauth", "transport": "streamable_http",
        "auth_type": "oauth",
    })

    add_form = json.dumps(asyncio.run(remote_mcp.components(orch, "owner", [], {})))
    none_form = json.dumps(asyncio.run(remote_mcp.components(
        orch, "owner", [], {"edit": none_connector["id"]})))
    oauth_form = json.dumps(asyncio.run(remote_mcp.components(
        orch, "owner", [], {"edit": oauth_connector["id"]})))

    assert '"label": "Streamable HTTP"' in add_form
    assert '"label": "Legacy SSE"' in add_form
    assert '"label": "Bearer token"' in add_form
    assert '"value": "streamable_http"' in add_form
    assert '"value": "sse"' in add_form
    assert '"visible_when": {"field": "auth_type", "equals": "bearer", "default": "none"}' in add_form
    assert '"visible_when": {"field": "auth_type", "equals": "oauth", "default": "none"}' in add_form
    assert "clear_oauth_client_credentials" not in add_form
    assert "clear_oauth_client_credentials" not in none_form
    assert "clear_oauth_client_credentials" in oauth_form


def test_surface_disabled_state_is_fail_closed(monkeypatch):
    monkeypatch.setattr(remote_mcp, "_enabled", lambda: False)
    orch = _orch()
    result = asyncio.run(remote_mcp.render(orch, "owner", [], {}))
    components = asyncio.run(remote_mcp.components(orch, "owner", [], {}))
    action = asyncio.run(remote_mcp._delete(orch, None, "owner", [], {}))
    assert "disabled" in result
    assert "alert" in json.dumps(components).lower()
    assert "disabled" in action[2].lower()


def test_surface_save_delete_toggle_and_error_paths(monkeypatch):
    orch = _orch()
    monkeypatch.setattr(remote_mcp, "_enabled", lambda: True)
    save = asyncio.run(remote_mcp._save(orch, None, "owner", [], {"fields": {
        "name": "Example", "url": "https://mcp.example/mcp", "transport": "streamable_http",
        "auth_type": "none", "ignored": {"nested": "value"},
    }}))
    assert "Remote MCP server saved" in save[2]
    connector_id = remote_mcp_connectors.list_connectors(orch, "owner")[0]["id"]
    edited = asyncio.run(remote_mcp._edit(orch, None, "owner", [], {"connector_id": connector_id}))
    assert edited[1] == {"edit": connector_id}
    toggled = asyncio.run(remote_mcp._toggle_connector(
        orch, None, "owner", [], {"connector_id": connector_id, "enabled": False}))
    assert "status saved" in toggled[2]
    deleted = asyncio.run(remote_mcp._delete(orch, None, "owner", [], {"connector_id": connector_id}))
    assert "deleted" in deleted[2]

    invalid = asyncio.run(remote_mcp._save(orch, None, "owner", [], {"fields": {
        "name": "Example", "url": "http://bad.example", "transport": "sse", "auth_type": "none",
    }}))
    assert "HTTPS" in invalid[2]
    missing_edit = asyncio.run(remote_mcp._edit(orch, None, "owner", [], {"connector_id": "bad"}))
    assert missing_edit[1] == {}
    assert "not found" in missing_edit[2].lower()


def test_surface_test_toggle_and_unknown_error_paths(monkeypatch):
    orch = _orch()
    monkeypatch.setattr(remote_mcp, "_enabled", lambda: True)
    saved = remote_mcp_connectors.save_connector(orch, "owner", {
        "name": "Example", "url": "https://mcp.example/mcp", "transport": "streamable_http",
        "auth_type": "none",
    })
    async def discover(*_args, **_kwargs):
        return [{"name": "echo", "description": "Echo", "inputSchema": {"type": "object"}}]

    monkeypatch.setattr(remote_mcp_connectors, "discover_tools", discover)
    refreshed = asyncio.run(remote_mcp._test(orch, None, "owner", [], {"connector_id": saved["id"]}))
    assert "Found 1 tools" in refreshed[2]
    assert remote_mcp_connectors.get_connector(orch, "owner", saved["id"])["tools"]["echo"] is False
    toggled = asyncio.run(remote_mcp._toggle(orch, None, "owner", [], {
        "connector_id": saved["id"], "tool_name": "echo", "enabled": True,
    }))
    assert "Tool selection saved" in toggled[2]

    def unexpected(*_args):
        raise RuntimeError("sensitive detail")

    monkeypatch.setattr(remote_mcp_connectors, "refresh_tools", unexpected)
    failed = asyncio.run(remote_mcp._test(orch, None, "owner", [], {"connector_id": saved["id"]}))
    assert "connector action failed" in failed[2].lower()
    assert "sensitive detail" not in failed[2]
    malformed = asyncio.run(remote_mcp._toggle(orch, None, "owner", [], {"bad": "payload"}))
    assert "not found" in malformed[2].lower()


def test_surface_fields_error_type_and_oauth_failure_sanitization(monkeypatch):
    assert remote_mcp._fields({"fields": {"x": " value ", "none": None, "nested": [], "n": 3}}) == {
        "x": "value", "n": "3",
    }
    assert remote_mcp._fields(None) == {}
    assert "connector action failed" in remote_mcp._notice_for_error(RuntimeError("secret")).lower()

    orch = _orch()
    monkeypatch.setattr(remote_mcp, "_enabled", lambda: True)
    monkeypatch.setattr("orchestrator.mcp_authz.canonical_public_base_url", lambda: "https://deep.example")

    async def failure(*_args):
        raise RuntimeError("private token")

    monkeypatch.setattr("orchestrator.remote_mcp_oauth.begin_authorization", failure)
    result = asyncio.run(remote_mcp._oauth_start(orch, None, "owner", [], {"connector_id": "a" * 32}))
    assert "connector action failed" in result[2].lower()
    assert "private token" not in result[2]


def test_components_render_saved_connector_and_edit_form(monkeypatch):
    orch = _orch()
    monkeypatch.setattr(remote_mcp, "_enabled", lambda: True)
    saved = remote_mcp_connectors.save_connector(orch, "owner", {
        "name": "Fixture", "url": "https://mcp.example/sse", "transport": "sse",
        "auth_type": "oauth", "oauth_client_id": "client-id",
        "tools_catalog": [{"name": "echo", "description": "Echo", "inputSchema": {"type": "object"}}],
        "tools": {"echo": True},
    })
    components = asyncio.run(remote_mcp.components(orch, "owner", [], {"edit": saved["id"]}))
    rendered = json.dumps(components)
    assert "Fixture" in rendered
    assert "chrome_remote_mcp_oauth_start" in rendered
    assert "chrome_remote_mcp_toggle_tool" in rendered
    assert "Edit MCP server" in rendered
    assert "client-id" in rendered

    invalid = asyncio.run(remote_mcp.components(orch, "owner", [], {"edit": "not-a-valid-id"}))
    assert "Add MCP server" in json.dumps(invalid)


def test_feature_flag_reads_remote_mcp_flag(monkeypatch):
    from shared.feature_flags import flags

    monkeypatch.setattr(flags, "is_enabled", lambda key: key == "remote_mcp")
    assert remote_mcp._enabled() is True
    monkeypatch.setattr(flags, "is_enabled", lambda _key: False)
    assert remote_mcp._enabled() is False


def test_edit_submission_cannot_recreate_connector_deleted_during_save(monkeypatch):
    orch = _orch()
    monkeypatch.setattr(remote_mcp, "_enabled", lambda: True)
    saved = remote_mcp_connectors.save_connector(orch, "owner", {
        "name": "Old", "url": "https://mcp.example/mcp", "transport": "streamable_http", "auth_type": "none",
    })
    manager = orch.credential_manager
    get_credential = manager.get_credential
    raced = False

    def race(owner, agent, key):
        nonlocal raced
        raw = get_credential(owner, agent, key)
        if not raced:
            raced = True
            manager.delete_credential(owner, agent, key)
        return raw

    monkeypatch.setattr(manager, "get_credential", race)
    result = asyncio.run(remote_mcp._save(orch, None, "owner", [], {
        "connector_id": saved["id"], "fields": {
            "name": "Updated", "url": "https://mcp.example/mcp", "transport": "streamable_http", "auth_type": "none",
        },
    }))
    assert "saved" not in result[2].lower()
    assert remote_mcp_connectors.list_connectors(orch, "owner") == []
