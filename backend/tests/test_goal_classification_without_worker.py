"""Covers orchestrator/goal_verification.classify_goal: a local-project classification becomes an
action contract when no local coding worker is configured, so GLM-only deployments can complete
project requests on outcome evidence instead of waiting for proofs only the worker can produce.
"""

import pytest

from orchestrator.goal_verification import classify_goal

PROJECT = {"project_id": "project", "computer_id": "mac", "computer_name": "Mac",
           "folder_status": "granted", "primary_root": "/work/demo", "workspace_roots": [{"path": "/work"}]}
REQUEST = "Create and test the local game"


@pytest.mark.asyncio
async def test_local_project_contract_is_kept_when_the_worker_is_configured(monkeypatch):
    monkeypatch.setenv("GOAL_CODEX_WORKER_PATH", "/opt/codex/bin/codex")
    contract = await classify_goal(None, None, REQUEST, PROJECT, "chat-a")
    assert contract.kind == "local_project"
    assert "tests" in contract.required_categories


@pytest.mark.asyncio
async def test_local_project_contract_becomes_an_action_contract_without_a_worker(monkeypatch):
    monkeypatch.setenv("GOAL_CODEX_WORKER_PATH", "")
    contract = await classify_goal(None, None, REQUEST, PROJECT, "chat-b")
    assert contract.kind == "action"
    assert contract.required_categories == ("outcome",)
    assert contract.blocked_reason is None
