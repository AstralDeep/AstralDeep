"""088 Agents native projection preserves web authorization and personal state."""
import json
from dataclasses import replace

import pytest

from orchestrator import chrome_events
from orchestrator.projection_surfaces import agents as surface
from shared.feature_flags import flags
from tests.chrome.test_chrome_surface import FakeOrch as TransportOrch
from tests.chrome.test_native_audit_088 import _nodes
from tests.chrome.test_surface_agents import FakeSkill, make_orch


def _actions(components):
    actions = []
    for node in _nodes(components):
        if node.get("type") == "button":
            actions.append(node)
        if node.get("type") == "param_picker":
            actions.extend(node.get("actions", []))
    return actions


def _action(components, action):
    return next(node for node in _actions(components) if node.get("action") == action)


def _fields(components, action="chrome_perms_save"):
    form = next(node for node in _nodes(components)
                if node.get("type") == "param_picker" and (
                    node.get("submit_action") == action
                    or any(item.get("action") == action for item in node.get("actions", []))))
    return {field["name"]: field for field in form["fields"]}


def _transport(orch, device="android", roles=("user",)):
    transport = TransportOrch(device=device, roles=roles)
    orch.ws, orch.ui_sessions, orch.sent, orch.rote = (
        transport.ws, transport.ui_sessions, transport.sent, transport.rote)
    orch._safe_send = transport._safe_send
    return orch


@pytest.mark.parametrize("params,expected,excluded", [
    (None, "Alpha Agent", "Beta Agent"),
    ([], "Alpha Agent", "Beta Agent"),
    ({"tab": "bogus"}, "Alpha Agent", "Beta Agent"),
    ({"tab": "public"}, "Beta Agent", "Alpha Agent"),
])
async def test_list_filters_match_web_and_hide_drafts(params, expected, excluded):
    orch = make_orch()
    orch.history.db.ownership["ghost"] = {"owner_email": "alice@example.com", "is_public": True}
    components = await surface.components(orch, "u1", ["admin"], params)
    encoded = json.dumps(components)
    assert expected in encoded and excluded not in encoded and "Ghost Draft" not in encoded
    assert "Owned by me" in encoded and "Drafts" in encoded
    assert _action(components, "chrome_agent_enabled")["payload"]["tab"] == (
        "public" if isinstance(params, dict) and params.get("tab") == "public" else "mine")


async def test_owned_public_agent_appears_in_both_tabs_with_personal_disabled_state():
    orch = make_orch()
    orch.history.db.ownership["alpha"]["is_public"] = True
    orch.history.db.disabled.add("alpha")
    for tab in ("mine", "public"):
        components = await surface.components(orch, "u1", [], {"tab": tab})
        assert "Alpha Agent" in json.dumps(components)
        assert "Yours" in json.dumps(components) and "Disabled by you" in json.dumps(components)
        enable = next(node for node in _actions(components)
                      if node.get("action") == "chrome_agent_enabled"
                      and node["payload"]["agent_id"] == "alpha")
        assert enable["label"] == "Enable" and enable["payload"]["enabled"] is True


@pytest.mark.parametrize("owner,roles,visibility,safe", [
    ("u1", ["user"], True, True),
    ("other", ["user"], False, False),
    ("other", ["owner"], False, False),
    ("other", ["admin"], False, True),
])
async def test_personal_configuration_is_separate_from_visibility_and_trust(owner, roles, visibility, safe):
    orch = make_orch()
    components = await surface.components(orch, owner, roles, {
        "agent_id": "alpha", "tab": "public", "owner_id": "u1", "roles": ["admin"],
    })
    actions = {node.get("action") for node in _actions(components)}
    assert ("chrome_visibility_set" in actions) is visibility
    assert ("chrome_safe_set" in actions) is safe
    assert _fields(components) and _fields(components, "chrome_credentials_save")
    enable = _action(components, "chrome_agent_enabled")["payload"]
    assert enable == {"agent_id": "alpha", "enabled": False, "tab": "public", "detail": True}
    back = next(node for node in _actions(components) if node.get("label") == "Back to agents")
    assert back["payload"]["params"] == {"tab": "public"}


async def test_detail_uses_same_owner_scoped_query_as_web_and_unknown_safe_hides_only_trust(monkeypatch):
    orch = make_orch()
    repository = orch.plane_repository_source.plane_repositories.agent_management
    original = repository.get_detail_context
    calls = []

    def query(transaction, **kwargs):
        calls.append(kwargs)
        return replace(original(transaction, **kwargs), safe_known=False)

    monkeypatch.setattr(repository, "get_detail_context", query)
    await surface.render(orch, "u1", ["admin"], {"agent_id": "alpha"})
    components = await surface.components(orch, "u1", ["admin"], {"agent_id": "alpha"})
    assert calls == [{"owner_id": "u1", "agent_id": "alpha"}] * 2
    actions = {node.get("action") for node in _actions(components)}
    assert "chrome_safe_set" not in actions and "chrome_visibility_set" in actions
    assert _fields(components)


async def test_permission_masters_and_tool_defaults_match_web_effective_resolution():
    orch = make_orch()
    components = await surface.components(orch, "u1", [], {"agent_id": "alpha"})
    fields = _fields(components)
    assert fields["__scope::tools:read"]["default"] is True
    assert fields["get_data::tools:read"]["default"] is True
    assert fields["__scope::tools:write"]["default"] is False
    assert fields["write_data::tools:write"]["visible_when"] == {
        "field": "__scope::tools:write", "equals": True, "default": False,
    }
    orch.tool_permissions.scopes["tools:write"] = True
    fields = _fields(await surface.components(orch, "u1", [], {"agent_id": "alpha"}))
    assert fields["__scope::tools:write"]["default"] is True
    assert fields["write_data::tools:write"]["default"] is False


@pytest.mark.parametrize("public,feature,expected", [(True, True, True), (False, True, False), (True, False, False)])
async def test_safe_default_requires_feature_public_and_keeps_explicit_optout(monkeypatch, public, feature, expected):
    orch = make_orch()
    orch.history.db.safe["alpha"] = True
    orch.history.db.ownership["alpha"]["is_public"] = public
    orch.tool_permissions.per_tool = {"write_data": {"tools:write": False}}
    orch.tool_permissions.scopes = {}
    monkeypatch.setitem(flags._flags, "safe_agents", feature)
    fields = _fields(await surface.components(orch, "u1", [], {"agent_id": "alpha"}))
    assert fields["get_data::tools:read"]["default"] is expected
    assert fields["write_data::tools:write"]["default"] is False


async def test_destructive_and_nonconfigurable_tools_remain_readable_before_grants():
    orch = make_orch()
    orch.tool_permissions.per_tool = {}
    orch.tool_permissions.scope_map["special"] = "custom:scope"
    orch.agent_cards["alpha"].skills[1].metadata["destructive"] = "always"
    orch.agent_cards["alpha"].skills.append(FakeSkill(
        "special", "An input-dependent operation", "custom:scope", metadata={"destructive": {"by_action": ["delete"]}}))
    components = await surface.components(orch, "u1", [], {"agent_id": "alpha"})
    assert "Destructive" in _fields(components)["write_data::tools:write"]["label"]
    encoded = json.dumps(components)
    assert "special" in encoded and "Not configurable" in encoded and "Sometimes destructive" in encoded
    assert all(field["default"] is False for field in _fields(components).values())
    orch.tool_permissions.scope_map = {"special": "custom:scope"}
    components = await surface.components(orch, "u1", [], {"agent_id": "alpha"})
    assert "special" in json.dumps(components) and "An input-dependent operation" in json.dumps(components)


async def test_credentials_project_declared_and_stored_metadata_without_reading_values(monkeypatch):
    orch = make_orch()
    orch.agent_cards["alpha"].metadata["required_credentials"] = [
        {"key": "optional_key", "label": "Optional credential", "required": False},
        "new_key", {"name": "new_key"}, {}, 5,
    ]

    def forbidden(*args):
        raise AssertionError("Credential values must never be requested to render settings")

    monkeypatch.setattr(orch.credential_manager, "get_agent_credentials_encrypted", forbidden)
    fields = _fields(await surface.components(orch, "u1", [], {"agent_id": "alpha"}), "chrome_credentials_save")
    assert list(fields) == ["optional_key", "new_key", "api_key"]
    assert "Optional" in fields["optional_key"]["label"]
    assert "stored" in fields["api_key"]["label"]
    assert all(field["kind"] == "password" and not field.get("default") for field in fields.values())
    assert _action(await surface.components(orch, "u1", [], {"agent_id": "alpha"}),
                   "chrome_credential_delete")["payload"]["key"] == "api_key"


@pytest.mark.parametrize("provider,linked_agent,subject", [
    ("orcid", "alpha", "0000-0000-0000-0001"),
    ("orcid", "beta", None),
    ("other", "alpha", None),
])
async def test_external_identity_is_same_agent_verified_status_or_explicit_web_handoff(provider, linked_agent, subject):
    orch = make_orch()
    orch.agent_cards["alpha"].metadata["external_identity"] = {"provider": provider}
    orch.history.db.preferences["u1"] = {"verified_external_identities": {
        "orcid": {"subject": "0000-0000-0000-0001", "issuer": "https://orcid.org", "verified_by_agent": linked_agent},
    }}
    encoded = json.dumps(await surface.components(orch, "u1", [], {"agent_id": "alpha"}))
    assert ("0000-0000-0000-0001" in encoded) is bool(subject)
    if provider == "orcid" and not subject:
        assert "web client" in encoded
    assert "/external-identities/orcid/start" not in encoded


async def test_missing_detail_has_notice_and_back_without_queries(monkeypatch):
    orch = make_orch()

    async def forbidden(*args):
        raise AssertionError("Missing agent cannot trigger owner context reads")

    monkeypatch.setattr(surface, "_detail_context", forbidden)
    components = await surface.components(orch, "u1", [], {"agent_id": "missing", "tab": "public"})
    assert "Agent not found" in json.dumps(components)
    assert _action(components, "chrome_open")["payload"]["params"] == {"tab": "public"}


@pytest.mark.parametrize("device", ["android", "ios", "macos"])
async def test_real_chrome_dispatch_roundtrips_permission_save_and_detail_enable(device):
    orch = _transport(make_orch(), device)
    await chrome_events.handle_chrome_event(orch, orch.ws, "chrome_open", {
        "surface": "agents", "params": {"agent_id": "alpha", "tab": "public"},
    }, "u1")
    frame = orch.sent[-1]
    assert frame["type"] == "chrome_surface" and frame["surface_key"] == "agents"
    fields = {name: value["default"] for name, value in _fields(frame["components"]).items()}
    fields["__scope::tools:read"] = False
    await chrome_events.handle_chrome_event(orch, orch.ws, "chrome_perms_save", {
        "agent_id": "alpha", "tab": "public", "fields": fields,
    }, "u1")
    assert ("get_data", "tools:read", False) in orch.tool_permissions.set_calls
    frame = orch.sent[-1]
    assert "Permissions saved" in json.dumps(frame)
    assert _fields(frame["components"])["get_data::tools:read"]["default"] is False
    payload = _action(frame["components"], "chrome_agent_enabled")["payload"]
    await chrome_events.handle_chrome_event(orch, orch.ws, "chrome_agent_enabled", payload, "u1")
    assert _fields(orch.sent[-1]["components"])
    assert orch.tool_permissions.disabled_calls[-1] == ("u1", "alpha", True)


async def test_native_visibility_forgery_rejected_by_existing_handler():
    orch = _transport(make_orch(), roles=("admin",))
    await chrome_events.handle_chrome_event(orch, orch.ws, "chrome_visibility_set", {
        "agent_id": "alpha", "is_public": True, "owner_email": "alice@example.com",
    }, "other")
    assert not orch.history.db.calls
    assert "Only the agent owner" in json.dumps(orch.sent[-1])


async def test_native_repository_failure_is_visible_without_leaking_detail(monkeypatch):
    orch = _transport(make_orch())

    async def broken(*args):
        raise RuntimeError("private database connection detail")

    monkeypatch.setattr(surface, "_list_context", broken)
    await chrome_events.handle_chrome_event(orch, orch.ws, "chrome_open", {"surface": "agents"}, "u1")
    encoded = json.dumps(orch.sent[-1])
    assert "error" in encoded.lower() and "private database connection detail" not in encoded
