"""Tests for the BYO transport boundary (backend/orchestrator/tool_permissions.py,
user_agents.py): undeclared-tool denial for foreign hosts, declared-only scope
tracking, and refusal of forged or reserved agent ids.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator.tool_permissions import ToolPermissionManager  # noqa: E402
from orchestrator import user_agents as ua  # noqa: E402
from orchestrator.user_agents import UserAgentRegistry  # noqa: E402
from tests.helpers.voice_plane_runtime import isolated_plane_runtime  # noqa: E402

OWNER = "byo058adv_owner"
FOREIGN = "byo058adv_foreign"
UA_ID = "byo058adv-myagent"
FOREIGN_UA = "byo058adv-theiragent"


@pytest.fixture()
def plane_env():
    with isolated_plane_runtime("byo_transport") as runtime:
        registry = UserAgentRegistry(
            plane_runtime=runtime,
            plane_repositories=runtime.repositories,
        )
        ua.create_user_agent(
            registry,
            agent_id=UA_ID,
            owner_user_id=OWNER,
            display_name="Mine",
        )
        ua.create_user_agent(
            registry,
            agent_id=FOREIGN_UA,
            owner_user_id=FOREIGN,
            display_name="Theirs",
        )
        permissions = ToolPermissionManager(
            plane_runtime=runtime,
            plane_repositories=runtime.repositories,
            user_agent_registry=registry,
        )
        yield SimpleNamespace(registry=registry, permissions=permissions)


def test_undeclared_tool_denied_for_foreign_host_fail_closed(plane_env):
    tp = plane_env.permissions
    tp.register_tool_scopes(UA_ID, {"greet": "tools:read"})
    assert tp.is_tool_allowed(FOREIGN, UA_ID, "greet") is False
    assert tp.is_tool_allowed(FOREIGN, UA_ID, "exfiltrate_secrets") is False
    assert tp.is_tool_allowed(FOREIGN, UA_ID, "read_files") is False


def test_permission_layer_tracks_only_declared_tool_scopes(plane_env):
    tp = plane_env.permissions
    tp.register_tool_scopes(UA_ID, {"greet": "tools:write"})
    assert tp.get_tool_scope(UA_ID, "greet") == "tools:write"
    assert "run_shell" not in tp.get_tool_scope_map(UA_ID)
    assert tp.get_tool_scope(UA_ID, "run_shell") == "tools:read"


def _reserved():
    return frozenset({"general-1", "general", "weather-1", "summarizer-1"})


def test_register_frame_claiming_another_users_agent_id_refused(plane_env):
    ua.mark_validated(plane_env.registry, FOREIGN_UA, "0.1.0")
    ok, reason = ua.authorize_registration(plane_env.registry, OWNER, FOREIGN_UA,
                                           reserved_ids=_reserved())
    assert ok is False
    assert "different user" in reason


def test_register_frame_claiming_builtin_id_refused(plane_env):
    ok, reason = ua.authorize_registration(plane_env.registry, OWNER, "general-1",
                                           reserved_ids=_reserved())
    assert ok is False
    assert "built-in" in reason or "reserved" in reason
    ok2, reason2 = ua.authorize_registration(plane_env.registry, OWNER, "general-1")
    assert ok2 is False
    assert "registry record" in reason2


def test_register_frame_claiming_reserved_pseudo_agent_id_refused(plane_env):
    for reserved_id in ("__orchestrator__", "__scheduler__", "__memory__",
                        "__subtasks__", "__anything_at_all"):
        ok, reason = ua.authorize_registration(plane_env.registry, OWNER, reserved_id,
                                               reserved_ids=_reserved())
        assert ok is False, f"{reserved_id} should be refused"
        assert "reserved" in reason


def test_missing_owner_or_agent_id_refused(plane_env):
    assert ua.authorize_registration(plane_env.registry, "", UA_ID)[0] is False
    assert ua.authorize_registration(plane_env.registry, OWNER, "")[0] is False


def test_owner_binding_positive_control(plane_env):
    ua.mark_validated(plane_env.registry, UA_ID, "0.1.0")
    ok, reason = ua.authorize_registration(
        plane_env.registry, OWNER, UA_ID, reserved_ids=_reserved()
    )
    assert ok is True, reason


def test_owner_binding_refused_before_validation(plane_env):
    row = ua.get_user_agent(plane_env.registry, UA_ID)
    assert row["status"] == "authoring"
    ok, reason = ua.authorize_registration(
        plane_env.registry, OWNER, UA_ID, reserved_ids=_reserved()
    )
    assert ok is False
    assert "not ready to run" in reason


def test_owner_binding_refused_when_revalidation_required(plane_env):
    ua.mark_validated(plane_env.registry, UA_ID, "0.1.0")
    ua.mark_revalidation_required(plane_env.registry, UA_ID, True)
    ok, reason = ua.authorize_registration(
        plane_env.registry, OWNER, UA_ID, reserved_ids=_reserved()
    )
    assert ok is False
    assert "Analyze" in reason
