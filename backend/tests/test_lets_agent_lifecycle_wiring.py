from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orchestrator.agent_lifecycle import AgentLifecycleManager, GENERATED
from orchestrator.lets_lifecycle import LifecycleConvergence


class LifecycleStore:
    def __init__(self, events: list[str], draft: dict[str, Any]) -> None:
        self.events = events
        self.draft = draft

    def get_draft_agent(self, draft_id: str) -> dict[str, Any] | None:
        return self.draft if self.draft.get("id") == draft_id else None

    def update_draft_agent(self, draft_id: str, **values: object) -> None:
        assert draft_id == self.draft["id"]
        self.draft.update(values)

    def get_user(self, user_id: str) -> dict[str, str]:
        return {"id": user_id, "email": "owner@example.test"}

    def set_agent_ownership(self, *_args: object, **_kwargs: object) -> None:
        self.events.append("ownership")

    def delete_draft_agent(self, draft_id: str) -> None:
        assert draft_id == self.draft["id"]
        self.events.append("db_delete")

    def purge_agent_state(self, **_kwargs: object) -> int:
        self.events.append("permission_purge")
        return 1


class Process:
    returncode = None

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def poll(self) -> None:
        return None

    def terminate(self, **_values: object) -> None:
        self.events.append("process_terminate")


class Supervisor:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def spawn(self, **_values: object) -> Process:
        self.events.append("process_spawn")
        return Process(self.events)


class Coordinator:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.service = SimpleNamespace(
            config=SimpleNamespace(
                mode="shadow",
                executor_instance_id="executor-a",
            )
        )

    async def admit_new_runtime(self, **_values: object) -> LifecycleConvergence:
        self.events.append("lets_admit")
        return LifecycleConvergence(protected=False)

    async def quiesce_current(self, **_values: object) -> LifecycleConvergence:
        self.events.append("lets_quiesce")
        return LifecycleConvergence(protected=False)

    async def close_current(self, **_values: object) -> LifecycleConvergence:
        self.events.append("lets_close")
        return LifecycleConvergence(protected=False)

    async def revoke_current(self, **_values: object) -> LifecycleConvergence:
        self.events.append("lets_revoke")
        return LifecycleConvergence(protected=False)


def _manager(tmp_path: Path, events: list[str]) -> AgentLifecycleManager:
    draft = {
        "id": "draft-a",
        "user_id": "owner-a",
        "agent_name": "Agent A",
        "agent_slug": "agent_a",
        "origin": "server",
        "status": GENERATED,
        "port": None,
    }
    manager = AgentLifecycleManager(
        LifecycleStore(events, draft),
        orchestrator=None,
        process_supervisor=Supervisor(events),
    )
    manager._agents_dir = str(tmp_path)
    manager.governed_lifecycle = Coordinator(events)  # type: ignore[assignment]
    return manager


@pytest.mark.asyncio
async def test_dynamic_admission_commits_before_physical_process_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    manager = _manager(tmp_path, events)
    agent_dir = tmp_path / "agent_a"
    agent_dir.mkdir()
    (agent_dir / "agent_a_agent.py").write_text("print('ready')\n", encoding="utf-8")
    (agent_dir / "mcp_tools.py").write_text(
        "TOOL_REGISTRY = {\n"
        "    'search': {'scope': 'tools:search'},\n"
        "    'read': {'scope': 'tools:read'},\n"
        "}\n",
        encoding="utf-8",
    )

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("orchestrator.agent_lifecycle.asyncio.sleep", no_sleep)
    result = await manager.start_draft_agent("draft-a")

    assert result["status"] == "testing"
    assert events.index("lets_admit") < events.index("process_spawn")
    assert manager._declared_scopes_from_tools_file(
        str(agent_dir / "mcp_tools.py")
    ) == ("tools:read", "tools:search")


@pytest.mark.asyncio
async def test_pause_fences_authority_before_unregister_and_process_stop(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    manager = _manager(tmp_path, events)
    manager._draft_processes["draft-a"] = Process(events)

    await manager.stop_draft_agent("draft-a")

    assert events[0] == "lets_quiesce"
    assert events.index("lets_quiesce") < events.index("process_terminate")


@pytest.mark.asyncio
async def test_delete_revokes_before_local_record_and_permission_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    manager = _manager(tmp_path, events)

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("orchestrator.agent_lifecycle.asyncio.sleep", no_sleep)
    assert await manager.delete_draft("draft-a") is True

    assert events.index("lets_quiesce") < events.index("lets_revoke")
    assert events.index("lets_revoke") < events.index("db_delete")
    assert events.index("db_delete") < events.index("permission_purge")
