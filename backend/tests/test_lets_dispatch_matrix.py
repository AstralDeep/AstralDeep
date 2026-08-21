"""Feature 074 T177: every physical path reaches the governed final seam."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.governed_dispatch import DispatchRuntime, GovernedFinalDispatch
from tests.lets_conformance_support import (
    AUTHORIZED_EFFECT,
    FINAL_ARGUMENTS,
    Plane,
    build_rig,
    invoke_executor,
)


_DISPATCH_PATHS = [
    ("REST", "rest", "server_dynamic"),
    ("WebSocket", "websocket", "server_dynamic"),
    ("A2A", "a2a", "server_dynamic"),
    ("MCP", "mcp", "server_dynamic"),
    ("background", "background", "server_dynamic"),
    ("scheduled", "scheduled", "server_dynamic"),
    ("chained", "chained", "server_dynamic"),
    ("recursive", "chained", "server_dynamic"),
    ("component", "websocket", "server_dynamic"),
    ("probe", "rest", "server_dynamic"),
    ("poll", "stream", "server_dynamic"),
    ("push", "stream", "server_dynamic"),
    ("in-process", "mcp", "server_dynamic"),
    ("generated", "mcp", "server_dynamic"),
    ("BYO", "websocket", "byo_user"),
    ("remote", "a2a", "external"),
    ("external", "a2a", "external"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path_name", "channel", "population"),
    _DISPATCH_PATHS,
    ids=[item[0] for item in _DISPATCH_PATHS],
)
async def test_actual_final_dispatch_matrix(
    tmp_path,
    monkeypatch,
    path_name: str,
    channel: str,
    population: str,
) -> None:
    recorder = SimpleNamespace(record=AsyncMock())
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: recorder)
    rig = build_rig(tmp_path, name=path_name.casefold().replace("-", "_"))
    governed = population in {"server_dynamic", "byo_user"}
    if population != "server_dynamic":
        rig.binding.population = population

        async def resolve(_agent_id: str, _owner_id: str | None) -> DispatchRuntime:
            return DispatchRuntime(
                owner_id="owner-a",
                agent_id="agent-a",
                population=population,
                runtime_id="runtime-a" if governed else None,
                runtime_generation=3 if governed else None,
                executor_audience="executor-a" if governed else None,
                executor_conformant=governed,
                dispatch_posture=(
                    "protected_executor" if governed else "dispatch_mediated_only"
                ),
            )

        rig.dispatch = GovernedFinalDispatch.active(
            gateway=rig.authorization,
            plane=Plane(),
            authority_repository=rig.repository,
            runtime_resolver=resolve,
        )

    effects: list[str] = []

    def physical_effect() -> str:
        effects.append(path_name)
        return f"{path_name}-result"

    def invoke(capabilities: dict[str, object]) -> str:
        if governed:
            return invoke_executor(rig, capabilities, actuator=physical_effect)
        assert capabilities == {}
        return physical_effect()

    try:
        result = await rig.dispatch.execute(
            owner_id="owner-a",
            agent_id="agent-a",
            tool_id="clinical.search_v2",
            scope="tools:read",
            channel=channel,
            audit_correlation_id=f"audit-{path_name}",
            final_arguments=FINAL_ARGUMENTS,
            authorized_effect=AUTHORIZED_EFFECT,
            invoke=invoke,
        )
    finally:
        rig.close()

    assert result == f"{path_name}-result"
    assert effects == [path_name]
    if governed:
        assert len(rig.warden.calls) == 1
        evidence = rig.warden.calls[0]["evidence"]
        assert isinstance(evidence, dict)
        assert evidence["channel"] == channel
        assert rig.coordinator.events == ["intent", "receipt", "claim", "succeeded"]
        assert len(rig.coordinator.receipts) == 1
        assert len(rig.coordinator.claims) == 1
        assert rig.store.status().claim_sequence == 1
    else:
        assert rig.warden.calls == []
        assert rig.repository.calls == []
        assert rig.coordinator.receipts == []
        assert rig.coordinator.claims == []
        assert rig.store.status().claim_sequence == 0
