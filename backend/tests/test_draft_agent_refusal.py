"""Checks that a draft agent start on the personal and server profiles never reports confinement, states the unconfined launch in its log, status and creation card, and refuses to start at all when resource limit setup fails.
Runs orchestrator/agent_lifecycle.py and orchestrator/agentic_creation.py against the fake store and supervisor from test_lets_agent_lifecycle_wiring.py."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from orchestrator import agent_lifecycle, agentic_creation, sandbox
from orchestrator.agent_lifecycle import DraftLaunchRefused, draft_confinement
from tests.test_lets_agent_lifecycle_wiring import _manager

PROFILES = ["personal", "server"]


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("orchestrator.agent_lifecycle.asyncio.sleep", no_sleep)


def prepared(tmp_path: Path, events: list[str]):
    manager = _manager(tmp_path, events)
    agent_dir = tmp_path / "agent_a"
    agent_dir.mkdir()
    (agent_dir / "agent_a_agent.py").write_text("print('ready')\n", encoding="utf-8")
    (agent_dir / "mcp_tools.py").write_text("TOOL_REGISTRY = {'read': {'scope': 'tools:read'}}\n", encoding="utf-8")
    return manager


@pytest.mark.parametrize("profile", PROFILES)
async def test_a_started_draft_agent_is_reported_unconfined_with_its_limits(tmp_path, monkeypatch, caplog, profile):
    monkeypatch.setenv("ASTRAL_NODE_PROFILE", profile)
    events: list[str] = []
    manager = prepared(tmp_path, events)
    with caplog.at_level(logging.WARNING, logger=agent_lifecycle.logger.name):
        result = await manager.start_draft_agent("draft-a")
    assert events.count("process_spawn") == 1
    assert result["confinement"]["confined"] is False
    assert result["confinement"]["resource_limits"] is True
    assert "runs unconfined" in result["confinement"]["notice"]
    assert any("runs unconfined" in record.getMessage() for record in caplog.records)


async def test_a_draft_agent_started_with_the_sandbox_flag_off_says_it_has_no_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("FF_SANDBOX_CODEGEN", "false")
    manager = prepared(tmp_path, [])
    result = await manager.start_draft_agent("draft-a")
    assert result["confinement"] == draft_confinement(False)
    assert "no resource limits" in result["confinement"]["notice"]


@pytest.mark.parametrize("profile", PROFILES)
async def test_a_failure_setting_up_resource_limits_refuses_the_start_on_every_profile(tmp_path, monkeypatch, profile):
    monkeypatch.setenv("ASTRAL_NODE_PROFILE", profile)

    def broken(_limits):
        raise OSError("limits unavailable")

    monkeypatch.setattr(sandbox, "make_preexec", broken)
    events: list[str] = []
    manager = prepared(tmp_path, events)
    with pytest.raises(DraftLaunchRefused):
        await manager.start_draft_agent("draft-a")
    assert "process_spawn" not in events and "lets_admit" not in events
    assert manager.db.draft["status"] == agent_lifecycle.ERROR
    assert "resource limits" in manager.db.draft["error_message"]


def test_the_launch_path_has_no_quiet_unsandboxed_fallback() -> None:
    assert "launching unsandboxed" not in Path(agent_lifecycle.__file__).read_text(encoding="utf-8")


def test_the_creation_card_carries_the_unconfined_notice() -> None:
    notice = draft_confinement(True)["notice"]
    card = agentic_creation.creation_card({"id": "d1", "agent_name": "A", "description": "d"}, {"status": "passed", "summary": "ok"}, note=notice)
    assert notice in str(card)
