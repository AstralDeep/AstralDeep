"""027 click-through fix: skill toggles must write the permission row that
``is_tool_allowed`` actually honors (per-kind first, legacy NULL outranked)."""
from orchestrator.tool_permissions import VALID_SCOPES, ToolPermissionManager


class FakePolicyRepository:
    def __init__(self):
        self.calls = []

    def set_tool_override(self, _transaction, **kwargs):
        self.calls.append(("set_tool_override", kwargs))

    def clear_tool_override(self, _transaction, **kwargs):
        self.calls.append(("clear_tool_override", kwargs))


class FakePolicyContext:
    def __init__(self):
        self.repository = FakePolicyRepository()

    def call(self, method, **kwargs):
        return method(None, **kwargs)


def _manager(scope_map):
    tp = ToolPermissionManager.__new__(ToolPermissionManager)
    tp._policy = FakePolicyContext()
    tp._tool_scope_map = scope_map
    return tp


def test_valid_scopes_include_files():
    """tools:files tools were uncontrollable — the scope now exists."""
    assert "tools:files" in VALID_SCOPES


def test_set_skill_enabled_writes_per_kind_row_and_clears_legacy():
    tp = _manager({"general-1": {"search_wikipedia": "tools:search"}})
    tp.set_skill_enabled("u1", "general-1", "search_wikipedia", False)
    assert tp._policy.repository.calls == [
        (
            "set_tool_override",
            {
                "owner_id": "u1",
                "agent_id": "general-1",
                "tool_name": "search_wikipedia",
                "permission_kind": "tools:search",
                "enabled": False,
                "updated_at": tp._policy.repository.calls[0][1]["updated_at"],
            },
        ),
        (
            "clear_tool_override",
            {
                "owner_id": "u1",
                "agent_id": "general-1",
                "tool_name": "search_wikipedia",
                "permission_kind": None,
            },
        ),
    ]


def test_set_skill_enabled_falls_back_for_unknown_scope():
    tp = _manager({"weird-1": {"odd_tool": "tools:quantum"}})
    tp.set_skill_enabled("u1", "weird-1", "odd_tool", False)
    [call] = tp._policy.repository.calls
    assert call[0] == "set_tool_override"
    assert call[1]["permission_kind"] is None
    assert call[1]["enabled"] is False
