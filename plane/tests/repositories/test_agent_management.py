"""Tests for astralplane.repositories.agent_management: list/detail context queries are
bounded, owner-scoped, and fail closed on corrupt or truncated data.
"""

from __future__ import annotations

import json

import pytest
from _support import ScriptedTransaction

from astralplane.repositories import RepositoryDataError
from astralplane.repositories.agent_management import AgentManagementRepository


def test_list_context_is_two_query_bounded_and_detached() -> None:
    transaction = ScriptedTransaction(
        one=[
            {
                "email": "owner@example.test",
                "preferences": json.dumps(
                    {"disabled_agents": ["agent-b", "agent-a", "agent-a"]}
                ),
            }
        ],
        all_rows=[
            (
                {
                    "agent_id": "agent-a",
                    "owner_email": "author@example.test",
                    "is_public": True,
                    "created_at": 1,
                    "updated_at": 2,
                },
            )
        ],
    )

    context = AgentManagementRepository().get_list_context(
        transaction,
        owner_id="owner-1",
        ownership_limit=10,
    )

    assert context.email == "owner@example.test"
    assert context.disabled_agent_ids == ("agent-a", "agent-b")
    assert context.ownership[0].is_public
    assert len(transaction.calls) == 2
    assert transaction.calls[0][2] == ("owner-1",)
    assert transaction.calls[1][2] == (11,)


def test_detail_context_is_three_query_typed_and_owner_scoped() -> None:
    preferences = {
        "disabled_agents": ["agent-1"],
        "verified_external_identities": {
            "orcid": {
                "subject": "0000-0001",
                "issuer": "https://orcid.example",
                "verified_at": 100,
                "verified_by_agent": "agent-1",
            }
        },
    }
    transaction = ScriptedTransaction(
        one=[
            {
                "email": "owner@example.test",
                "preferences": preferences,
                "ownership_agent_id": "agent-1",
                "owner_email": "owner@example.test",
                "is_public": False,
                "ownership_created_at": 1,
                "ownership_updated_at": 2,
                "trust_agent_id": "agent-1",
                "is_safe": True,
            }
        ],
        all_rows=[
            ({"credential_key": "API_KEY"},),
            (
                {
                    "state_kind": "scope",
                    "user_id": "owner-1",
                    "agent_id": "agent-1",
                    "scope": "tools:read",
                    "tool_name": None,
                    "permission_kind": None,
                    "enabled": True,
                    "updated_at": 3,
                },
                {
                    "state_kind": "override",
                    "user_id": "owner-1",
                    "agent_id": "agent-1",
                    "scope": None,
                    "tool_name": "read_records",
                    "permission_kind": "tools:read",
                    "enabled": False,
                    "updated_at": 4,
                },
            ),
        ],
    )

    context = AgentManagementRepository().get_detail_context(
        transaction,
        owner_id="owner-1",
        agent_id="agent-1",
        credential_limit=10,
        scope_limit=10,
        tool_override_limit=10,
        external_identity_limit=10,
    )

    assert context.disabled
    assert context.safe_known and context.is_safe
    assert context.ownership is not None and not context.ownership.is_public
    assert context.credential_keys == ("API_KEY",)
    assert context.scope_states[0].scope == "tools:read"
    assert context.tool_override_states[0].tool_name == "read_records"
    assert not context.tool_override_states[0].enabled
    assert context.external_identity_links[0].provider == "orcid"
    assert len(transaction.calls) == 3
    assert transaction.calls[0][2] == ("owner-1", "agent-1")
    assert transaction.calls[1][2] == ("owner-1", "agent-1", 11)
    assert transaction.calls[2][2] == (
        "owner-1",
        "agent-1",
        11,
        "owner-1",
        "agent-1",
        11,
    )


def test_contexts_fail_closed_on_corruption_or_truncation() -> None:
    with pytest.raises(RepositoryDataError, match="disabled_agents"):
        AgentManagementRepository().get_list_context(
            ScriptedTransaction(
                one=[{"email": None, "preferences": '{"disabled_agents":"bad"}'}],
                all_rows=[()],
            ),
            owner_id="owner-1",
        )

    with pytest.raises(RepositoryDataError, match="configured bound"):
        AgentManagementRepository().get_list_context(
            ScriptedTransaction(
                one=[{"email": None, "preferences": None}],
                all_rows=[
                    (
                        {
                            "agent_id": "a",
                            "owner_email": "a@example.test",
                            "is_public": False,
                            "created_at": 1,
                            "updated_at": 1,
                        },
                        {
                            "agent_id": "b",
                            "owner_email": "b@example.test",
                            "is_public": False,
                            "created_at": 1,
                            "updated_at": 1,
                        },
                    )
                ],
            ),
            owner_id="owner-1",
            ownership_limit=1,
        )

    with pytest.raises(RepositoryDataError, match="scope state"):
        AgentManagementRepository().get_detail_context(
            ScriptedTransaction(
                one=[
                    {
                        "email": None,
                        "preferences": None,
                        "ownership_agent_id": None,
                        "trust_agent_id": None,
                    }
                ],
                all_rows=[
                    (),
                    (
                        {
                            "state_kind": "scope",
                            "user_id": "owner-1",
                            "agent_id": "agent-1",
                            "scope": "tools:read",
                            "tool_name": None,
                            "permission_kind": None,
                            "enabled": "yes",
                            "updated_at": 1,
                        },
                    ),
                ],
            ),
            owner_id="owner-1",
            agent_id="agent-1",
        )

    with pytest.raises(RepositoryDataError, match="owner fence"):
        AgentManagementRepository().get_detail_context(
            ScriptedTransaction(
                one=[
                    {
                        "email": None,
                        "preferences": None,
                        "ownership_agent_id": None,
                        "trust_agent_id": None,
                    }
                ],
                all_rows=[
                    (),
                    (
                        {
                            "state_kind": "override",
                            "user_id": "another-owner",
                            "agent_id": "agent-1",
                            "scope": None,
                            "tool_name": "read_records",
                            "permission_kind": "tools:read",
                            "enabled": True,
                            "updated_at": 1,
                        },
                    ),
                ],
            ),
            owner_id="owner-1",
            agent_id="agent-1",
        )

    with pytest.raises(RepositoryDataError, match="tool-override inventory"):
        AgentManagementRepository().get_detail_context(
            ScriptedTransaction(
                one=[
                    {
                        "email": None,
                        "preferences": None,
                        "ownership_agent_id": None,
                        "trust_agent_id": None,
                    }
                ],
                all_rows=[
                    (),
                    tuple(
                        {
                            "state_kind": "override",
                            "user_id": "owner-1",
                            "agent_id": "agent-1",
                            "scope": None,
                            "tool_name": f"tool-{index}",
                            "permission_kind": "tools:read",
                            "enabled": True,
                            "updated_at": index,
                        }
                        for index in range(2)
                    ),
                ],
            ),
            owner_id="owner-1",
            agent_id="agent-1",
            tool_override_limit=1,
        )

    with pytest.raises(RepositoryDataError, match="invalid row kind"):
        AgentManagementRepository().get_detail_context(
            ScriptedTransaction(
                one=[
                    {
                        "email": None,
                        "preferences": None,
                        "ownership_agent_id": None,
                        "trust_agent_id": None,
                    }
                ],
                all_rows=[
                    (),
                    (
                        {
                            "state_kind": "unexpected",
                            "user_id": "owner-1",
                            "agent_id": "agent-1",
                            "scope": None,
                            "tool_name": None,
                            "permission_kind": None,
                            "enabled": False,
                            "updated_at": None,
                        },
                    ),
                ],
            ),
            owner_id="owner-1",
            agent_id="agent-1",
        )
