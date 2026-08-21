"""Feature 058 (US3, T015) — transport/registration adversarial suite.

The scenarios NOT already covered by ``test_byo_tunnel.py`` (foreign-owner
registration refused, per-owner flood cap, honest-offline, no-delegation-token,
deliver-to-host, bundle-never-to-a-tab, register_ui host marking, soft-delete,
list isolation) or ``test_byo_boundary_adversarial.py`` (owner isolation +
owner-allow baseline). Here we pin:

  (a) an UNDECLARED tool over the tunnel is denied fail-closed at the untrusted
      boundary (a non-owner host cannot reach ANY tool — declared or not), and
      the permission layer tracks ONLY declared tool→scope records; and
  (b) a FORGED agent_id inside the register frame is refused because the owner
      is derived from the authenticated session ``sub`` and the agent_id is
      matched against the orchestrator's OWN registry — never anything the card
      presents (``user_agents.authorize_registration``).

Audit-on-denial (T015c) is covered — and its current GAPS documented — in
``test_byo_audit_completeness.py`` (T035).
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

# NOTE: agent ids must NOT start with "__" — that prefix is the reserved
# pseudo-agent namespace ``authorize_registration`` refuses outright. User ids
# are unconstrained.
OWNER = "byo058adv_owner"
FOREIGN = "byo058adv_foreign"
UA_ID = "byo058adv-myagent"           # OWNER's agent, declares only "greet"
FOREIGN_UA = "byo058adv-theiragent"   # FOREIGN's agent


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


# ---------------------------------------------------------------------------
# (a) Undeclared tool over the tunnel
# ---------------------------------------------------------------------------

def test_undeclared_tool_denied_for_foreign_host_fail_closed(plane_env):
    """A registered user-agent declares only ``greet``. A DIFFERENT owner's host
    (the untrusted party at the boundary) that dispatches an undeclared tool ``Y``
    is denied fail-closed by owner isolation — declared OR undeclared, a non-owner
    reaches nothing."""
    tp = plane_env.permissions
    tp.register_tool_scopes(UA_ID, {"greet": "tools:read"})   # only "greet" declared
    # The declared tool AND an undeclared tool are both denied to a non-owner.
    assert tp.is_tool_allowed(FOREIGN, UA_ID, "greet") is False
    assert tp.is_tool_allowed(FOREIGN, UA_ID, "exfiltrate_secrets") is False
    assert tp.is_tool_allowed(FOREIGN, UA_ID, "read_files") is False


def test_permission_layer_tracks_only_declared_tool_scopes(plane_env):
    """The tool→scope registry records ONLY the tools the card declared; an
    undeclared tool has no declared scope record (it resolves to the neutral
    ``tools:read`` default, never inheriting a stronger declared scope)."""
    tp = plane_env.permissions
    tp.register_tool_scopes(UA_ID, {"greet": "tools:write"})
    # Declared tool keeps its declared scope.
    assert tp.get_tool_scope(UA_ID, "greet") == "tools:write"
    # An undeclared tool is NOT in the registry — it gets the neutral default,
    # so no undeclared name can silently borrow the declared tool's authority.
    assert "run_shell" not in tp.get_tool_scope_map(UA_ID)
    assert tp.get_tool_scope(UA_ID, "run_shell") == "tools:read"


# ---------------------------------------------------------------------------
# (b) Forged identity inside the register frame
# ---------------------------------------------------------------------------

def _reserved():
    """The built-in/public id set the tunnel registration path passes through
    (mirrors ``register_agent``: ``db._FIRST_PARTY_PUBLIC_AGENT_IDS``)."""
    return frozenset({"general-1", "general", "weather-1", "summarizer-1"})


def test_register_frame_claiming_another_users_agent_id_refused(plane_env):
    """The owner authenticates on their OWN socket but the card claims a
    DIFFERENT user's live agent id. Owner is derived from the session ``sub``;
    the registry vouches the id belongs to FOREIGN, so it is refused — the card
    can never bind an id the session does not own (FR-002/FR-015)."""
    ua.mark_validated(plane_env.registry, FOREIGN_UA, "0.1.0")
    ok, reason = ua.authorize_registration(plane_env.registry, OWNER, FOREIGN_UA,
                                           reserved_ids=_reserved())
    assert ok is False
    assert "different user" in reason


def test_register_frame_claiming_builtin_id_refused(plane_env):
    """A card claiming a built-in/public id (e.g. ``general-1``) is refused: it
    both collides with a reserved id AND has no user-agent registry record."""
    # Collision branch (id is in the reserved set).
    ok, reason = ua.authorize_registration(plane_env.registry, OWNER, "general-1",
                                           reserved_ids=_reserved())
    assert ok is False
    assert "built-in" in reason or "reserved" in reason
    # And even without the reserved set, a built-in id has no user_agent row.
    ok2, reason2 = ua.authorize_registration(plane_env.registry, OWNER, "general-1")
    assert ok2 is False
    assert "registry record" in reason2


def test_register_frame_claiming_reserved_pseudo_agent_id_refused(plane_env):
    """A card claiming a reserved ``__*`` pseudo-agent id (e.g.
    ``__orchestrator__``) is refused before any registry lookup — the meta-tool
    namespace is structurally unreachable to a user agent (Constitution H)."""
    for reserved_id in ("__orchestrator__", "__scheduler__", "__memory__",
                        "__subtasks__", "__anything_at_all"):
        ok, reason = ua.authorize_registration(plane_env.registry, OWNER, reserved_id,
                                               reserved_ids=_reserved())
        assert ok is False, f"{reserved_id} should be refused"
        assert "reserved" in reason


def test_missing_owner_or_agent_id_refused(plane_env):
    """No authenticated owner, or an empty agent id, refuses fail-closed."""
    assert ua.authorize_registration(plane_env.registry, "", UA_ID)[0] is False
    assert ua.authorize_registration(plane_env.registry, OWNER, "")[0] is False


def test_owner_binding_positive_control(plane_env):
    """Sanity: the OWNER registering their OWN validated agent id is admitted —
    the refusals above are targeted, not a blanket deny."""
    ua.mark_validated(plane_env.registry, UA_ID, "0.1.0")
    ok, reason = ua.authorize_registration(
        plane_env.registry, OWNER, UA_ID, reserved_ids=_reserved()
    )
    assert ok is True, reason


def test_owner_binding_refused_before_validation(plane_env):
    """An agent still in ``authoring`` status (not yet Analyze-validated) cannot
    register inward, even for its rightful owner (fail-closed status gate)."""
    row = ua.get_user_agent(plane_env.registry, UA_ID)
    assert row["status"] == "authoring"
    ok, reason = ua.authorize_registration(
        plane_env.registry, OWNER, UA_ID, reserved_ids=_reserved()
    )
    assert ok is False
    assert "not ready to run" in reason


def test_owner_binding_refused_when_revalidation_required(plane_env):
    """A constitution-version bump sets ``revalidation_required``; the agent must
    re-pass Analyze before it can route again (FR-028)."""
    ua.mark_validated(plane_env.registry, UA_ID, "0.1.0")
    ua.mark_revalidation_required(plane_env.registry, UA_ID, True)
    ok, reason = ua.authorize_registration(
        plane_env.registry, OWNER, UA_ID, reserved_ids=_reserved()
    )
    assert ok is False
    assert "Analyze" in reason
