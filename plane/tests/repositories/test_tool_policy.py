"""Tests for astralplane.repositories.tool_policy: owner-scoped scope and override
state, legacy-override backfill that never clobbers an existing choice, and
transactional permission cleanup.
"""

from __future__ import annotations

import json

import pytest
from _support import Result, ScriptedTransaction

from astralplane.repositories import RepositoryDataError
from astralplane.repositories.tool_policy import ToolPolicyStateRepository


def test_scope_and_override_state_is_owner_scoped_and_neutral() -> None:
    repository = ToolPolicyStateRepository()
    scope_rows = (
        {
            "user_id": "owner-1",
            "agent_id": "agent-1",
            "scope": "custom:bounded",
            "enabled": True,
            "updated_at": 3,
        },
    )
    scopes = repository.list_scopes(
        ScriptedTransaction(all_rows=[scope_rows]),
        owner_id="owner-1",
        agent_id="agent-1",
    )
    assert scopes[0].scope == "custom:bounded"
    assert scopes[0].enabled

    override = {
        "user_id": "owner-1",
        "agent_id": "agent-1",
        "tool_name": "search",
        "permission_kind": "tools:search",
        "enabled": False,
        "updated_at": 4,
    }
    written = repository.set_tool_override(
        ScriptedTransaction(one=[override]),
        owner_id="owner-1",
        agent_id="agent-1",
        tool_name="search",
        permission_kind="tools:search",
        enabled=False,
        updated_at=4,
    )
    assert written.owner_id == "owner-1"
    assert not written.enabled


def test_legacy_override_backfill_never_overwrites_an_existing_choice() -> None:
    repository = ToolPolicyStateRepository()
    inserted = ScriptedTransaction(execute=[Result(rowcount=1)])
    conflicted = ScriptedTransaction(execute=[Result(rowcount=0)])

    assert repository.create_tool_override_if_absent(
        inserted,
        owner_id="owner-1",
        agent_id="agent-1",
        tool_name="search",
        permission_kind="tools:search",
        enabled=True,
        updated_at=7,
    )
    assert not repository.create_tool_override_if_absent(
        conflicted,
        owner_id="owner-1",
        agent_id="agent-1",
        tool_name="search",
        permission_kind=None,
        enabled=False,
        updated_at=8,
    )
    assert "DO NOTHING" in inserted.fetch_sql()
    assert "DO UPDATE" not in inserted.fetch_sql()
    with pytest.raises(RepositoryDataError):
        repository.create_tool_override_if_absent(
            ScriptedTransaction(execute=[Result(rowcount=-1)]),
            owner_id="owner-1",
            agent_id="agent-1",
            tool_name="search",
            permission_kind=None,
            enabled=False,
            updated_at=8,
        )


def test_set_scopes_is_deterministic_and_validates_boolean_state() -> None:
    repository = ToolPolicyStateRepository()
    rows = [
        {
            "user_id": "owner-1",
            "agent_id": "agent-1",
            "scope": "tools:read",
            "enabled": True,
            "updated_at": 5,
        },
        {
            "user_id": "owner-1",
            "agent_id": "agent-1",
            "scope": "tools:write",
            "enabled": False,
            "updated_at": 5,
        },
    ]
    transaction = ScriptedTransaction(one=rows)
    result = repository.set_scopes(
        transaction,
        owner_id="owner-1",
        agent_id="agent-1",
        scopes={"tools:write": False, "tools:read": True},
        updated_at=5,
    )
    assert tuple(scope.scope for scope in result) == ("tools:read", "tools:write")
    with pytest.raises(ValueError):
        repository.set_scopes(
            ScriptedTransaction(),
            owner_id="owner-1",
            agent_id="agent-1",
            scopes={"tools:read": 1},  # type: ignore[dict-item]
            updated_at=5,
        )


def test_tool_selection_updates_one_locked_preferences_document_without_lost_merge() -> None:
    repository = ToolPolicyStateRepository()
    transaction = ScriptedTransaction(
        one=[
            {
                "preferences": json.dumps(
                    {"theme": {"preset": "night"}, "tool_selection": {"other": ["x"]}}
                )
            }
        ],
        execute=[Result(), Result(rowcount=1)],
    )

    selected = repository.set_tool_selection(
        transaction,
        owner_id="owner-1",
        agent_id="agent-1",
        tool_names=("search", "read", "search"),
        updated_at=9,
    )

    assert selected == ("search", "read")
    assert "FOR UPDATE" in transaction.fetch_sql()
    encoded = transaction.calls[-1][2][0]  # type: ignore[index]
    written = json.loads(encoded)
    assert written["theme"] == {"preset": "night"}
    assert written["tool_selection"] == {
        "agent-1": ["search", "read"],
        "other": ["x"],
    }


def test_selection_clear_and_agent_disable_are_idempotent() -> None:
    repository = ToolPolicyStateRepository()
    no_selection = ScriptedTransaction(one=[{"preferences": "{}"}], execute=[Result()])
    assert not repository.clear_tool_selection(
        no_selection,
        owner_id="owner-1",
        agent_id="agent-1",
        updated_at=3,
    )
    already_disabled = ScriptedTransaction(
        one=[{"preferences": '{"disabled_agents":["agent-1"]}'}],
        execute=[Result()],
    )
    assert not repository.set_agent_disabled(
        already_disabled,
        owner_id="owner-1",
        agent_id="agent-1",
        disabled=True,
        updated_at=4,
    )


def test_corrupt_preferences_fail_closed_and_cleanup_stays_bounded() -> None:
    repository = ToolPolicyStateRepository()
    with pytest.raises(RepositoryDataError):
        repository.get_tool_selection(
            ScriptedTransaction(one=[{"preferences": "[]"}]),
            owner_id="owner-1",
            agent_id="agent-1",
        )
    transaction = ScriptedTransaction(execute=[Result(rowcount=2)])
    assert (
        repository.prune_agent_overrides(
            transaction, agent_id="agent-1", live_tool_names=("search", "read")
        )
        == 2
    )
    assert "tool_name NOT IN (%s, %s)" in transaction.fetch_sql()


def test_scope_inventory_override_clear_and_legacy_rows() -> None:
    repository = ToolPolicyStateRepository()
    scope = {
        "user_id": "owner-1",
        "agent_id": "agent-1",
        "scope": "tools:read",
        "enabled": True,
        "updated_at": None,
    }
    assert (
        repository.list_all_scopes(ScriptedTransaction(all_rows=[(scope,)]), owner_id="owner-1")[
            0
        ].updated_at
        is None
    )
    assert repository.has_any_enabled_scope(
        ScriptedTransaction(one=[{"present": 1}]), owner_id="owner-1"
    )
    assert not repository.has_any_enabled_scope(ScriptedTransaction(one=[None]), owner_id="owner-1")

    override = {
        "user_id": "owner-1",
        "agent_id": "agent-1",
        "tool_name": "read",
        "permission_kind": None,
        "enabled": False,
        "updated_at": None,
    }
    assert (
        repository.list_overrides(
            ScriptedTransaction(all_rows=[(override,)]),
            owner_id="owner-1",
            agent_id="agent-1",
        )[0].permission_kind
        is None
    )
    assert repository.clear_tool_override(
        ScriptedTransaction(one=[{"id": 1}]),
        owner_id="owner-1",
        agent_id="agent-1",
        tool_name="read",
        permission_kind=None,
    )
    assert not repository.clear_tool_override(
        ScriptedTransaction(one=[None]),
        owner_id="owner-1",
        agent_id="agent-1",
        tool_name="read",
        permission_kind="tools:read",
    )
    legacy = {
        "user_id": "owner-1",
        "agent_id": "agent-1",
        "tool_name": "read",
        "allowed": True,
        "updated_at": 3,
    }
    assert repository.list_legacy_permissions(
        ScriptedTransaction(all_rows=[(legacy,)]),
        owner_id="owner-1",
        agent_id="agent-1",
    )[0].allowed

    inventory = ScriptedTransaction(
        all_rows=[(({"user_id": "owner-1", "agent_id": "draft-agent-1"}),)]
    )
    scoped = repository.list_scoped_agent_owners_for_administration(
        inventory,
        agent_id_suffix="-1",
        limit=5,
    )
    assert (scoped[0].owner_id, scoped[0].agent_id) == (
        "owner-1",
        "draft-agent-1",
    )
    assert inventory.calls[0][2] == ("-1", "-1", 5)
    assert "SELECT DISTINCT user_id, agent_id" in inventory.fetch_sql()


def test_permission_cleanup_is_transactional_for_owner_and_agent() -> None:
    repository = ToolPolicyStateRepository()
    assert (
        repository.remove_agent_state(
            ScriptedTransaction(
                execute=[Result(rowcount=1), Result(rowcount=2), Result(rowcount=-1)]
            ),
            owner_id="owner-1",
            agent_id="agent-1",
        )
        == 3
    )
    assert (
        repository.remove_owner_state(
            ScriptedTransaction(
                execute=[Result(rowcount=2), Result(rowcount=1), Result(rowcount=1)]
            ),
            owner_id="owner-1",
        )
        == 4
    )
    transaction = ScriptedTransaction(execute=[Result(rowcount=5)])
    assert (
        repository.prune_agent_overrides(transaction, agent_id="agent-1", live_tool_names=()) == 5
    )
    assert "tool_name NOT IN" not in transaction.fetch_sql()


def test_selection_reads_clears_and_disabled_state_round_trip() -> None:
    repository = ToolPolicyStateRepository()
    preferences = {
        "tool_selection": {"agent-1": ["read", "search"]},
        "disabled_agents": ["agent-2"],
    }
    assert repository.get_tool_selection(
        ScriptedTransaction(one=[{"preferences": json.dumps(preferences)}]),
        owner_id="owner-1",
        agent_id="agent-1",
    ) == ("read", "search")
    assert (
        repository.get_tool_selection(
            ScriptedTransaction(one=[None]), owner_id="owner-1", agent_id="agent-1"
        )
        is None
    )
    assert (
        repository.get_tool_selection(
            ScriptedTransaction(one=[{"preferences": '{"tool_selection":[]}'}]),
            owner_id="owner-1",
            agent_id="agent-1",
        )
        is None
    )

    clear_tx = ScriptedTransaction(
        one=[{"preferences": json.dumps(preferences)}],
        execute=[Result(), Result(rowcount=1)],
    )
    assert repository.clear_tool_selection(
        clear_tx, owner_id="owner-1", agent_id="agent-1", updated_at=4
    )
    assert "agent-1" not in json.loads(clear_tx.calls[-1][2][0])["tool_selection"]  # type: ignore[index]

    assert repository.list_disabled_agents(
        ScriptedTransaction(one=[{"preferences": json.dumps(preferences)}]),
        owner_id="owner-1",
    ) == ("agent-2",)
    assert (
        repository.list_disabled_agents(ScriptedTransaction(one=[None]), owner_id="owner-1") == ()
    )

    add_tx = ScriptedTransaction(
        one=[{"preferences": "{}"}], execute=[Result(), Result(rowcount=1)]
    )
    assert repository.set_agent_disabled(
        add_tx,
        owner_id="owner-1",
        agent_id="agent-1",
        disabled=True,
        updated_at=5,
    )
    remove_tx = ScriptedTransaction(
        one=[{"preferences": '{"disabled_agents":["agent-1","agent-2"]}'}],
        execute=[Result(), Result(rowcount=1)],
    )
    assert repository.set_agent_disabled(
        remove_tx,
        owner_id="owner-1",
        agent_id="agent-1",
        disabled=False,
        updated_at=6,
    )
    assert json.loads(remove_tx.calls[-1][2][0])["disabled_agents"] == ["agent-2"]  # type: ignore[index]


def test_preference_shape_and_update_count_fail_closed() -> None:
    repository = ToolPolicyStateRepository()
    with pytest.raises(RepositoryDataError):
        repository.list_disabled_agents(
            ScriptedTransaction(one=[{"preferences": '{"disabled_agents":"agent-1"}'}]),
            owner_id="owner-1",
        )
    with pytest.raises(RepositoryDataError):
        repository.set_tool_selection(
            ScriptedTransaction(
                one=[{"preferences": "{}"}],
                execute=[Result(), Result(rowcount=0)],
            ),
            owner_id="owner-1",
            agent_id="agent-1",
            tool_names=("read",),
            updated_at=1,
        )
    with pytest.raises(ValueError):
        repository.set_tool_selection(
            ScriptedTransaction(),
            owner_id="owner-1",
            agent_id="agent-1",
            tool_names=(),
            updated_at=1,
        )
