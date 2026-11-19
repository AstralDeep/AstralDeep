"""Tests for gate-denial auditing through the protected dispatch (backend/verification/
drivers/in_process.py, Orchestrator.execute_authorized_tool): each refusal leaves a
failed tool.<name>.denied row naming its gate and detail, and the chain still verifies.
"""

from __future__ import annotations

import tempfile
import uuid

import pytest

from verification.config import RunConfig
from verification.drivers.in_process import InProcessDriver, _run_sync
from verification.isolation import Principal, make_principal
from verification.tests.conftest import INTEGRATION, run_async

pytestmark = INTEGRATION

_IDENTITY_AGENT = "identity-verif-1"
_DESTRUCTIVE_AGENT = "computer-use-1"


def _register(driver, agent_id, tool, scope, metadata=None):
    from shared.protocol import AgentCard, AgentSkill

    driver.orch.agent_cards[agent_id] = AgentCard(
        name=agent_id, description="verification fixture agent", agent_id=agent_id,
        skills=[AgentSkill(name=tool, description="fixture", id=tool,
                           input_schema={"type": "object", "properties": {}})],
        metadata=metadata or {},
    )
    driver.orch.tool_permissions.register_tool_scopes(agent_id, {tool: scope})


async def _dispatch(driver, principal, agent_id, tool, channel):
    chat_id = await _run_sync(driver.orch.history.create_chat, user_id=principal.user_id)
    return await driver.orch.execute_authorized_tool(
        claims=Principal(principal.user_id, list(principal.roles)).claims(),
        user_id=principal.user_id, agent_id=agent_id, tool_name=tool, arguments={},
        channel=channel, chat_id=chat_id,
    )


def _denied(rows):
    return [r for r in rows if r["action_type"].endswith(".denied")]


@pytest.fixture(scope="module")
def denials():
    config = RunConfig(
        mode="in_process", run_id=f"__verif__{uuid.uuid4().hex[:10]}",
        out_dir=tempfile.mkdtemp(prefix="verif_denials_"),
    )
    with pytest.MonkeyPatch.context() as patch:
        for name, value in (("ASTRAL_ENV", "development"), ("DELEGATION_REQUIRED", "false"),
                            ("FF_TAINT_TRACKING", "false"), ("FF_POLICY_ENGINE", "false"),
                            ("FF_ASI_COVERAGE", "false")):
            patch.setenv(name, value)
        return run_async(_collect(config))


async def _collect(config):
    driver = InProcessDriver(config)
    await driver.setup()
    try:
        _register(driver, _IDENTITY_AGENT, "identity_probe", "tools:read",
                  {"required_identity_claims": ["orcid"]})
        _register(driver, _DESTRUCTIVE_AGENT, "write_file", "tools:write")
        cases = {
            "permission": ("withheldP", driver.agent_id, "read_spreadsheet", "rest",
                           {"tools:read": False}),
            "identity": ("identityP", _IDENTITY_AGENT, "identity_probe", "rest",
                         {"tools:read": True}),
            "destructive": ("destructiveP", _DESTRUCTIVE_AGENT, "write_file", "mcp",
                            {"tools:write": True}),
        }
        out = {}
        for name, (key, agent, tool, channel, scopes) in cases.items():
            principal = driver._execution_principal(make_principal(config.run_id, key))
            await _run_sync(driver.orch.tool_permissions.set_agent_scopes,
                            principal.user_id, agent, scopes)
            response = await _dispatch(driver, principal, agent, tool, channel)
            rows, chain = await _run_sync(driver._read_audit, principal.user_id)
            out[name] = (response, rows, chain)
        return out
    finally:
        await driver.teardown()


@pytest.mark.parametrize(
    ("case", "tool", "gate", "detail"),
    [
        ("permission", "read_spreadsheet", "tool_permission", "tool_not_allowed"),
        ("identity", "identity_probe", "agent_identity", "identity_required"),
        ("destructive", "write_file", "unattended_destructive", "mcp_channel"),
    ],
)
def test_refusal_is_audited_with_its_gate(denials, case, tool, gate, detail):
    response, rows, chain = denials[case]
    assert response.error is not None
    denied = _denied(rows)
    assert len(denied) == 1
    row = denied[0]
    assert row["action_type"] == f"tool.{tool}.denied"
    assert row["event_class"] == "agent_tool_call"
    assert row["outcome"] == "failure"
    assert row["inputs_meta"]["gate"] == gate
    assert chain is True
    assert not any(r["outcome"] == "success" for r in rows)
    assert row["outcome_detail"] == detail
