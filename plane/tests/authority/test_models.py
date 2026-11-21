"""Tests for astralplane.authority.models: AgentAuthorityBinding immutability,
generation fencing, provisioning-intent identity rules, and terminal-state exactness.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from astralplane.authority.models import (
    AgentAuthorityBinding,
    AuthorityBindingState,
    AuthorityPopulation,
    pending_authority_identity,
)
from astralplane.errors import DomainValidationError

POLICY_DIGEST = "sha256:" + "1" * 64
MACHINE_DIGEST = "sha256:" + "2" * 64


def _binding(**changes: object) -> AgentAuthorityBinding:
    values: dict[str, object] = {
        "binding_id": "binding-1",
        "owner_id": "owner-1",
        "agent_id": "agent-1",
        "runtime_id": "runtime-1",
        "runtime_generation": 3,
        "population": AuthorityPopulation.SERVER_DYNAMIC,
        "tenant_id": "tenant-1",
        "envelope_id": "envelope-1",
        "warden_id": "warden-1",
        "lease_id": "lease-1",
        "lineage_id": "lineage-1",
        "subject_id": "agent-1",
        "policy_digest": POLICY_DIGEST,
        "machine_digest": MACHINE_DIGEST,
        "config_epoch": 7,
        "capabilities": ("astral.tools.read", "astral.tools.write"),
        "lease_sequence": 0,
        "lease_expires_at_ns": 1_800_000_000_000_000_000,
        "state": AuthorityBindingState.ACTIVE,
        "created_at": datetime(2026, 8, 14, 12, tzinfo=timezone(timedelta(hours=-4))),
        "updated_at": datetime(2026, 8, 14, 17, tzinfo=UTC),
        "version": 0,
    }
    values.update(changes)
    return AgentAuthorityBinding(**values)  # type: ignore[arg-type]


def test_binding_is_immutable_canonical_and_generation_fenced() -> None:
    binding = _binding()

    assert binding.created_at == datetime(2026, 8, 14, 16, tzinfo=UTC)
    assert binding.owner_agent_key == (
        "owner-1",
        "agent-1",
        AuthorityPopulation.SERVER_DYNAMIC,
    )
    assert binding.runtime_generation_key == (
        "owner-1",
        "agent-1",
        "runtime-1",
        3,
    )
    with pytest.raises(AttributeError):
        binding.version = 2  # type: ignore[misc]


def test_only_declared_terminal_states_are_terminal() -> None:
    terminals = {
        AuthorityBindingState.CLOSED,
        AuthorityBindingState.REVOKED,
        AuthorityBindingState.EXPIRED,
    }
    assert {state for state in AuthorityBindingState if state.terminal} == terminals


def test_provisioning_intent_reserves_deterministic_non_authoritative_identities() -> None:
    intent = AgentAuthorityBinding.provisioning_intent(
        binding_id="binding-pending",
        owner_id="owner-1",
        agent_id="agent-1",
        runtime_id="runtime-1",
        runtime_generation=3,
        population=AuthorityPopulation.SERVER_DYNAMIC,
        tenant_id="tenant-1",
        envelope_id="envelope-1",
        policy_digest=POLICY_DIGEST,
        machine_digest=MACHINE_DIGEST,
        config_epoch=7,
        capabilities=("astral.tools.read",),
        created_at=datetime(2026, 8, 14, 16, tzinfo=UTC),
    )

    assert intent.state is AuthorityBindingState.PROVISIONING
    assert intent.lease_sequence == 0
    assert intent.lease_expires_at_ns == 0
    assert (
        intent.warden_id,
        intent.lease_id,
        intent.lineage_id,
        intent.subject_id,
    ) == (
        pending_authority_identity(intent.binding_id, field="warden"),
        pending_authority_identity(intent.binding_id, field="lease"),
        pending_authority_identity(intent.binding_id, field="lineage"),
        pending_authority_identity(intent.binding_id, field="subject"),
    )

    with pytest.raises(DomainValidationError, match="not supported"):
        pending_authority_identity(intent.binding_id, field="unknown")  # type: ignore[arg-type]


def test_pending_remote_identities_are_limited_to_provisioning_or_closed() -> None:
    intent = AgentAuthorityBinding.provisioning_intent(
        binding_id="binding-pending",
        owner_id="owner-1",
        agent_id="agent-1",
        runtime_id="runtime-1",
        runtime_generation=3,
        population=AuthorityPopulation.SERVER_DYNAMIC,
        tenant_id="tenant-1",
        envelope_id="envelope-1",
        policy_digest=POLICY_DIGEST,
        machine_digest=MACHINE_DIGEST,
        config_epoch=7,
        capabilities=("astral.tools.read",),
        created_at=datetime(2026, 8, 14, 16, tzinfo=UTC),
    )

    with pytest.raises(DomainValidationError, match="issued lease metadata"):
        replace(intent, lease_expires_at_ns=1)

    abandoned = replace(
        intent,
        state=AuthorityBindingState.CLOSED,
        updated_at=intent.updated_at + timedelta(seconds=1),
        version=1,
    )
    assert abandoned.warden_id == intent.warden_id
    assert abandoned.lease_sequence == 0
    assert abandoned.lease_expires_at_ns == 0

    with pytest.raises(DomainValidationError, match="pending remote identity"):
        replace(intent, state=AuthorityBindingState.ACTIVE, lease_expires_at_ns=1)
    for state in (AuthorityBindingState.REVOKED, AuthorityBindingState.EXPIRED):
        with pytest.raises(DomainValidationError, match="pending remote identity"):
            replace(intent, state=state, lease_expires_at_ns=1)
    with pytest.raises(DomainValidationError, match="deterministic pending"):
        _binding(
            state=AuthorityBindingState.PROVISIONING,
            lease_expires_at_ns=0,
        )


@pytest.mark.parametrize(
    "field",
    [
        "binding_id",
        "owner_id",
        "agent_id",
        "runtime_id",
        "tenant_id",
        "envelope_id",
        "warden_id",
        "lease_id",
        "lineage_id",
        "subject_id",
    ],
)
def test_identifiers_fail_closed_without_echoing_values(field: str) -> None:
    invalid = " secret value "
    with pytest.raises(DomainValidationError) as caught:
        _binding(**{field: invalid})
    assert invalid not in str(caught.value)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"runtime_generation": 0}, "runtime generation"),
        ({"runtime_generation": True}, "runtime generation"),
        ({"config_epoch": 0}, "config epoch"),
        ({"lease_sequence": -1}, "lease sequence"),
        ({"lease_sequence": False}, "lease sequence"),
        ({"lease_expires_at_ns": 0}, "lease expiry"),
        ({"version": -1}, "version"),
    ],
)
def test_integer_fences_fail_closed(changes: dict[str, object], message: str) -> None:
    with pytest.raises(DomainValidationError, match=message):
        _binding(**changes)


@pytest.mark.parametrize("field", ["policy_digest", "machine_digest"])
@pytest.mark.parametrize("value", ["1" * 64, "sha256:" + "A" * 64, "sha256:short"])
def test_external_authority_digests_are_exact(field: str, value: str) -> None:
    with pytest.raises(DomainValidationError, match="canonical LETS SHA-256"):
        _binding(**{field: value})


@pytest.mark.parametrize(
    "capabilities",
    [
        (),
        ["astral.tools.read"],
        ("astral.tools.write", "astral.tools.read"),
        ("astral.tools.read", "astral.tools.read"),
        ("astral.tools.read", 3),
    ],
)
def test_capabilities_are_nonempty_sorted_unique_identifiers(
    capabilities: object,
) -> None:
    with pytest.raises(DomainValidationError, match="capabilities"):
        _binding(capabilities=capabilities)


def test_population_and_state_require_typed_contract_values() -> None:
    with pytest.raises(DomainValidationError, match="population"):
        _binding(population="server_dynamic")
    with pytest.raises(DomainValidationError, match="state"):
        _binding(state="active")


def test_timestamp_order_and_timezone_are_enforced() -> None:
    with pytest.raises(DomainValidationError, match="timezone-aware"):
        _binding(created_at=datetime(2026, 8, 14, 12))
    with pytest.raises(DomainValidationError, match="cannot precede"):
        _binding(
            created_at=datetime(2026, 8, 14, 18, tzinfo=UTC),
            updated_at=datetime(2026, 8, 14, 17, tzinfo=UTC),
        )


def test_replace_preserves_validation() -> None:
    binding = replace(_binding(), state=AuthorityBindingState.QUIESCENT, version=1)
    assert binding.state is AuthorityBindingState.QUIESCENT
    assert binding.version == 1
