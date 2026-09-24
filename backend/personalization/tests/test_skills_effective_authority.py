"""Tests for personalization/api.py and
orchestrator/projection_surfaces/personalization.py: the skills REST endpoint and
chrome/native components agree on effective, owner-bounded tool authorization.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from personalization import api
from orchestrator.projection_surfaces import personalization as surface


class Permissions:
    _tool_scope_map = {"helper": {"read": "tools:read"}}

    def __init__(self, authorized=True):
        self.authorized = authorized
        self.writes = []

    def get_tool_scope_map(self, _agent):
        return {"read": "tools:read"}

    def get_tool_scope(self, *_args):
        return "tools:read"

    def is_tool_allowed(self, *_args):
        return self.authorized

    def is_scope_enabled(self, *_args):
        raise AssertionError("the raw scope table is not effective authorization")

    def is_skill_authorized(self, user, agent, tool):
        assert (user, agent, tool) == ("alice", "helper", "read")
        return self.authorized

    def set_skill_enabled(self, *args):
        self.writes.append(args)


def request(tp):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        orchestrator=SimpleNamespace(tool_permissions=tp))))


def test_catalog_and_native_components_use_effective_authority():
    tp = Permissions()
    catalog = asyncio.run(api.list_skills(request(tp), user_id="alice"))
    assert catalog["skills"][0]["authorized"] and catalog["skills"][0]["enabled"]
    native = surface._components_skills(SimpleNamespace(tool_permissions=tp), "alice")
    assert "Enabled" in str(native) and "Disable" in str(native)
    tp.authorized = False
    assert "Agents & permissions" in str(surface._components_skills(SimpleNamespace(tool_permissions=tp), "alice"))


def test_rest_toggle_uses_effective_authority_and_denies_without_grant(monkeypatch):
    tp = Permissions()
    audit = AsyncMock()
    monkeypatch.setattr(api, "record_generic", audit)
    body = api.SkillToggleRequest(agent_id="helper", tool_name="read", enabled=True)
    result = asyncio.run(api.toggle_skill(body, request(tp), user_id="alice", payload={"sub": "alice"}))
    assert result["enabled"] and tp.writes == [("alice", "helper", "read", True)]
    tp.authorized = False
    with pytest.raises(HTTPException) as error:
        asyncio.run(api.toggle_skill(body, request(tp), user_id="alice", payload={"sub": "alice"}))
    assert error.value.status_code == 403 and len(tp.writes) == 1
    assert audit.await_count == 1
