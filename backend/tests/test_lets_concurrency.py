"""Feature 074 T176: binding-local ordering without global serialization."""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.governed_dispatch import DispatchRuntime, GovernedFinalDispatch
from orchestrator.lets_gateway import LETS_CALLER_CAPABILITY, LetsGatewayError
from tests.lets_conformance_support import (
    AUTHORIZED_EFFECT,
    FINAL_ARGUMENTS,
    build_rig,
    host_arguments,
    invoke_executor,
    signed_envelope,
)


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout=timeout)


@pytest.mark.asyncio
async def test_same_binding_physical_effects_are_strictly_ordered(
    tmp_path, monkeypatch
) -> None:
    recorder = SimpleNamespace(record=AsyncMock())
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: recorder)
    rig = build_rig(tmp_path)
    first_started = threading.Event()
    release_first = threading.Event()
    effects: list[str] = []

    def first_actuator() -> str:
        effects.append("first")
        rig.binding.lease_sequence = 1
        first_started.set()
        if not release_first.wait(2):
            raise TimeoutError("test did not release first binding effect")
        return "first-result"

    def second_actuator() -> str:
        effects.append("second")
        rig.binding.lease_sequence = 2
        return "second-result"

    async def execute(actuator) -> str:
        async def invoke(capabilities):
            return await asyncio.to_thread(
                invoke_executor,
                rig,
                capabilities,
                actuator=actuator,
            )

        return await rig.dispatch.execute(
            owner_id="owner-a",
            agent_id="agent-a",
            tool_id="clinical.search_v2",
            scope="tools:read",
            channel="background",
            audit_correlation_id="audit-same-binding",
            final_arguments=FINAL_ARGUMENTS,
            authorized_effect=AUTHORIZED_EFFECT,
            invoke=invoke,
        )

    first_task = asyncio.create_task(execute(first_actuator))
    second_task = None
    try:
        await _wait_until(first_started.is_set)
        second_task = asyncio.create_task(execute(second_actuator))
        await _wait_until(lambda: len(rig.repository.calls) == 2)
        assert len(rig.warden.calls) == 1
        assert effects == ["first"]
        assert not second_task.done()
        release_first.set()
        assert await first_task == "first-result"
        assert await second_task == "second-result"
    finally:
        release_first.set()
        if not first_task.done():
            first_task.cancel()
        if second_task is not None and not second_task.done():
            second_task.cancel()
        rig.close()

    assert effects == ["first", "second"]
    assert len(rig.warden.calls) == 2
    assert rig.store.status().claim_sequence == 2
    assert [claim[0].receipt.resulting_sequence for claim in rig.coordinator.claims] == [
        1,
        2,
    ]


@pytest.mark.asyncio
async def test_distinct_bindings_execute_concurrently_but_cannot_cross_claim(
    tmp_path, monkeypatch
) -> None:
    recorder = SimpleNamespace(record=AsyncMock())
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: recorder)
    rig = build_rig(tmp_path)
    binding_b = SimpleNamespace(
        **{
            **vars(rig.binding),
            "binding_id": "binding-b",
            "owner_id": "owner-b",
            "agent_id": "agent-b",
            "runtime_id": "runtime-b",
            "runtime_generation": 4,
            "lease_id": "lease-b",
            "subject_id": "agent-b",
        }
    )
    bindings = {"agent-a": rig.binding, "agent-b": binding_b}

    class Plane:
        @contextmanager
        def transaction(self):
            yield object()

    class Repository:
        def get_active_binding(self, _transaction, **values):
            return bindings[values["agent_id"]]

    async def resolve(agent_id: str, owner_id: str | None) -> DispatchRuntime:
        binding = bindings[agent_id]
        return DispatchRuntime(
            owner_id=owner_id,
            agent_id=agent_id,
            population="server_dynamic",
            runtime_id=binding.runtime_id,
            runtime_generation=binding.runtime_generation,
            executor_audience="executor-a",
            executor_conformant=True,
            dispatch_posture="protected_executor",
        )

    dispatch = GovernedFinalDispatch.active(
        gateway=rig.authorization,
        plane=Plane(),
        authority_repository=Repository(),
        runtime_resolver=resolve,
    )
    release = threading.Event()
    started = {"agent-a": threading.Event(), "agent-b": threading.Event()}
    effects: list[str] = []

    def actuator(agent_id: str) -> str:
        effects.append(agent_id)
        started[agent_id].set()
        if not release.wait(2):
            raise TimeoutError("test did not release independent binding effects")
        return agent_id

    async def execute(agent_id: str, owner_id: str, binding) -> str:
        async def invoke(capabilities):
            return await asyncio.to_thread(
                rig.executor.claim_and_invoke,
                metadata=capabilities[LETS_CALLER_CAPABILITY],
                actuator=lambda: actuator(agent_id),
                **host_arguments(
                    owner_id=owner_id,
                    binding_id=binding.binding_id,
                    lease_id=binding.lease_id,
                    lineage_id=binding.lineage_id,
                    agent_id=agent_id,
                    runtime_id=binding.runtime_id,
                    runtime_generation=binding.runtime_generation,
                ),
            )

        return await dispatch.execute(
            owner_id=owner_id,
            agent_id=agent_id,
            tool_id="clinical.search_v2",
            scope="tools:read",
            channel="websocket",
            audit_correlation_id=f"audit-{agent_id}",
            final_arguments=FINAL_ARGUMENTS,
            authorized_effect=AUTHORIZED_EFFECT,
            invoke=invoke,
        )

    tasks = [
        asyncio.create_task(execute("agent-a", "owner-a", rig.binding)),
        asyncio.create_task(execute("agent-b", "owner-b", binding_b)),
    ]
    try:
        await _wait_until(lambda: all(event.is_set() for event in started.values()))
        assert len(rig.warden.calls) == 2
        assert set(effects) == {"agent-a", "agent-b"}
        release.set()
        assert set(await asyncio.gather(*tasks)) == {"agent-a", "agent-b"}

        cross_owner_effects: list[str] = []
        with pytest.raises(LetsGatewayError, match="^executor_host_binding_mismatch$"):
            rig.executor.claim_and_invoke(
                metadata=signed_envelope(rig.signer).to_metadata(),
                actuator=lambda: cross_owner_effects.append("effect"),
                **host_arguments(
                    owner_id="owner-b",
                    binding_id="binding-b",
                    lease_id="lease-b",
                    agent_id="agent-b",
                    runtime_id="runtime-b",
                    runtime_generation=4,
                ),
            )
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        rig.close()

    assert cross_owner_effects == []
    assert rig.store.status().claim_sequence == 2
    assert len(rig.coordinator.claims) == 2
