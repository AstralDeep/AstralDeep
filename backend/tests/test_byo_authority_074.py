"""Exact v3 personal-agent executor-authority contract tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.byo_authority import (
    ByoRuntimeAuthority,
    ByoRuntimeAuthorityError,
    derive_byo_executor_audience,
)
from orchestrator.lets_lifecycle import LifecycleConvergence, LetsLifecycleError
from orchestrator.orchestrator import Orchestrator
from shared.protocol import RuntimeFence


HOST_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "22222222-2222-4222-8222-222222222222"
RUNTIME_ID = "33333333-3333-4333-8333-333333333333"


def _fence() -> RuntimeFence:
    return RuntimeFence(
        agent_id="ua-test-owner",
        host_id=HOST_ID,
        host_session_id=SESSION_ID,
        delivery_id="44444444-4444-4444-8444-444444444444",
        revision_id="55555555-5555-4555-8555-555555555555",
        runtime_instance_id=RUNTIME_ID,
        process_id=None,
        lifecycle_generation=7,
    )


def _binding(**overrides: object) -> object:
    values = {
        "binding_id": "binding-a",
        "lease_id": "lease-a",
        "lineage_id": "lineage-a",
        "owner_id": "owner-a",
        "agent_id": "ua-test-owner",
        "runtime_id": RUNTIME_ID,
        "runtime_generation": 7,
        "population": SimpleNamespace(value="byo_user"),
        "state": SimpleNamespace(value="active"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_executor_audience_has_one_cross_client_canonical_vector() -> None:
    assert derive_byo_executor_audience(HOST_ID, SESSION_ID) == (
        "astraldeep.byo-executor/v1:"
        "9e7551f451da68f7e25858d30b1947d3ee6befade36e6a91ee00f4b5cbd25f06"
    )


def test_active_binding_produces_only_the_exact_v3_authority_fields() -> None:
    authority = ByoRuntimeAuthority.from_active_binding(
        _binding(),
        fence=_fence(),
        owner_id="owner-a",
    )

    assert authority.to_dict() == {
        "owner_id": "owner-a",
        "binding_id": "binding-a",
        "lease_id": "lease-a",
        "lineage_id": "lineage-a",
        "population": "byo_user",
        "executor_audience": derive_byo_executor_audience(HOST_ID, SESSION_ID),
        "agent_id": "ua-test-owner",
        "runtime_instance_id": RUNTIME_ID,
        "lifecycle_generation": 7,
    }


@pytest.mark.parametrize(
    "overrides",
    (
        {"state": SimpleNamespace(value="quiescent")},
        {"population": SimpleNamespace(value="server_dynamic")},
        {"owner_id": "owner-b"},
        {"agent_id": "other-agent"},
        {"runtime_id": "66666666-6666-4666-8666-666666666666"},
        {"runtime_generation": 8},
        {"lease_id": ""},
        {"lineage_id": ""},
    ),
)
def test_nonmatching_binding_is_never_projected_into_delivery(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ByoRuntimeAuthorityError):
        ByoRuntimeAuthority.from_active_binding(
            _binding(**overrides),
            fence=_fence(),
            owner_id="owner-a",
        )


def test_audience_rejects_noncanonical_or_non_uuid_host_fences() -> None:
    with pytest.raises(ByoRuntimeAuthorityError, match="host_id_invalid"):
        derive_byo_executor_audience(
            "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
            SESSION_ID,
        )
    with pytest.raises(ByoRuntimeAuthorityError, match="host_session_id_invalid"):
        derive_byo_executor_audience(HOST_ID, "not-a-uuid")


@pytest.mark.asyncio
async def test_enforce_admission_caches_only_the_exact_active_binding() -> None:
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.lets_runtime = SimpleNamespace(
        config=SimpleNamespace(mode="enforce")
    )
    orchestrator._personal_agent_runtime_authorities = {}
    orchestrator._personal_agent_declared_scopes = AsyncMock(
        return_value=("tools:read",)
    )
    convergence = LifecycleConvergence(protected=True, binding=_binding())
    lifecycle = SimpleNamespace(
        admit_or_resume=AsyncMock(return_value=convergence)
    )
    orchestrator.governed_byo_lifecycle = lifecycle
    runtime = SimpleNamespace(fence=_fence())

    authority = await orchestrator._admit_personal_agent_runtime_authority(
        owner_user_id="owner-a",
        runtime=runtime,
        executor_conformant=True,
    )

    assert authority is not None
    assert orchestrator._personal_agent_runtime_authorities == {
        RUNTIME_ID: authority
    }
    lifecycle.admit_or_resume.assert_awaited_once_with(
        owner_user_id="owner-a",
        runtime=runtime,
        declared_scopes=("tools:read",),
        executor_conformant=True,
    )


@pytest.mark.asyncio
async def test_degraded_shadow_admission_is_explicitly_unprotected() -> None:
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.lets_runtime = SimpleNamespace(
        config=SimpleNamespace(mode="shadow")
    )
    orchestrator._personal_agent_runtime_authorities = {}
    orchestrator._personal_agent_declared_scopes = AsyncMock(
        return_value=("tools:read",)
    )
    orchestrator.governed_byo_lifecycle = SimpleNamespace(
        admit_or_resume=AsyncMock(side_effect=RuntimeError("warden unavailable"))
    )

    authority = await orchestrator._admit_personal_agent_runtime_authority(
        owner_user_id="owner-a",
        runtime=SimpleNamespace(fence=_fence()),
        executor_conformant=True,
    )

    assert authority is None
    assert orchestrator._personal_agent_runtime_authorities == {}


@pytest.mark.asyncio
async def test_enforce_admission_refuses_a_missing_lifecycle_runtime() -> None:
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.lets_runtime = SimpleNamespace(
        config=SimpleNamespace(mode="enforce")
    )
    orchestrator.governed_byo_lifecycle = None

    with pytest.raises(LetsLifecycleError, match="byo_lifecycle_unavailable"):
        await orchestrator._admit_personal_agent_runtime_authority(
            owner_user_id="owner-a",
            runtime=SimpleNamespace(fence=_fence()),
            executor_conformant=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    (("lease_id", "lease-b"), ("lineage_id", "lineage-b")),
)
async def test_renewal_rejects_changed_immutable_lease_or_lineage(
    caplog: pytest.LogCaptureFixture,
    field: str,
    value: str,
) -> None:
    authority = ByoRuntimeAuthority.from_active_binding(
        _binding(),
        fence=_fence(),
        owner_id="owner-a",
    )
    renew_if_due = AsyncMock(
        return_value=LifecycleConvergence(
            protected=True,
            binding=_binding(**{field: value}),
        )
    )
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.lets_runtime = SimpleNamespace(
        config=SimpleNamespace(default_ttl_seconds=60)
    )
    orchestrator.governed_byo_lifecycle = SimpleNamespace(
        renew_if_due=renew_if_due
    )
    orchestrator._personal_agent_runtime_authorities = {RUNTIME_ID: authority}

    with caplog.at_level("WARNING"):
        renewed = await orchestrator._renew_personal_agent_authorities()

    assert renewed == 0
    assert orchestrator._personal_agent_runtime_authorities == {
        RUNTIME_ID: authority
    }
    assert "BYO LETS lease renewal failed" in caplog.text
    assert "changed immutable authority identity" in caplog.text
    renew_if_due.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_retirement_is_fenced_to_exact_generation() -> None:
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.lets_runtime = SimpleNamespace(
        config=SimpleNamespace(mode="enforce")
    )
    retire = AsyncMock(return_value=LifecycleConvergence(protected=True))
    orchestrator.governed_byo_lifecycle = SimpleNamespace(
        retire_runtime=retire
    )

    await orchestrator._retire_personal_agent_runtime_authority(
        owner_user_id="owner-a",
        fence=_fence(),
    )

    retire.assert_awaited_once_with(
        owner_user_id="owner-a",
        agent_id="ua-test-owner",
        runtime_id=RUNTIME_ID,
        runtime_generation=7,
    )
