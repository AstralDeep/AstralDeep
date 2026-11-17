"""Tests for astralplane.authority.models and repositories/history.py: a framework
credential's independent owner-lifetime never gets structurally confused with Plane's
lineage-bound AgentAuthorityBinding delegation.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime

from astralplane.authority.models import (
    AgentAuthorityBinding,
    AuthorityBindingState,
    AuthorityPopulation,
)
from astralplane.repositories.history import FrameworkCredentialFence

POLICY_DIGEST = "sha256:" + "1" * 64
MACHINE_DIGEST = "sha256:" + "2" * 64


def _binding(**overrides: object) -> AgentAuthorityBinding:
    intent = AgentAuthorityBinding.provisioning_intent(
        binding_id="binding-1",
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
    return intent if not overrides else _replace_dataclass(intent, **overrides)


def _replace_dataclass(instance, **overrides):
    from dataclasses import replace

    return replace(instance, **overrides)


def _credential(**overrides: object) -> FrameworkCredentialFence:
    values = dict(
        owner_id="owner-1",
        credential_id="cred-1",
        token_hash="a" * 64,
        scopes=("operations.submit",),
        max_admissions=5,
        consumed_admissions=0,
        created_at=1_800_000_000,
        expires_at=1_800_003_600,
        revoked_at=None,
    )
    values.update(overrides)
    return FrameworkCredentialFence(**values)


def test_a_framework_credential_carries_no_parent_lineage_or_runtime_binding():
    credential_fields = {field.name for field in fields(FrameworkCredentialFence)}
    binding_fields = {field.name for field in fields(AgentAuthorityBinding)}
    lineage_only_fields = {
        "runtime_id",
        "runtime_generation",
        "lineage_id",
        "lease_id",
        "lease_sequence",
        "lease_expires_at_ns",
        "warden_id",
        "subject_id",
        "envelope_id",
        "config_epoch",
    }
    assert lineage_only_fields <= binding_fields
    assert lineage_only_fields.isdisjoint(credential_fields)
    assert credential_fields & binding_fields == {"owner_id", "created_at"}


def test_a_framework_credential_expiry_is_independent_of_any_lease():
    binding = _binding()
    credential = _credential()
    assert binding.lease_sequence == 0
    assert binding.lease_expires_at_ns == 0
    assert binding.state is AuthorityBindingState.PROVISIONING
    assert credential.expires_at > credential.created_at
    assert credential.max_admissions >= 1
    assert not hasattr(credential, "lease_id")
    assert not hasattr(credential, "runtime_generation")


def test_attenuating_a_binding_advances_its_own_lineage_never_a_credentials():
    first = _binding()
    renewed = _replace_dataclass(
        first,
        warden_id="warden-issued-1",
        lease_id="lease-issued-1",
        lineage_id="lineage-issued-1",
        subject_id="subject-issued-1",
        lease_sequence=first.lease_sequence + 1,
        lease_expires_at_ns=1_000,
        state=AuthorityBindingState.ACTIVE,
        version=first.version + 1,
    )
    assert renewed.binding_id == first.binding_id
    assert renewed.runtime_id == first.runtime_id
    assert renewed.runtime_generation == first.runtime_generation
    assert renewed.lease_sequence != first.lease_sequence
    assert renewed.lineage_id != first.lineage_id

    credential = _credential()
    assert credential.owner_id == first.owner_id
    foreign_identities = {renewed.lineage_id, renewed.runtime_id, renewed.lease_id}
    assert not any(
        getattr(credential, name, None) in foreign_identities
        for name in ("credential_id", "token_hash", "issuer_kind")
        if hasattr(credential, name)
    )


def test_two_credentials_for_the_same_owner_never_share_a_parent_reference():
    first = _credential(credential_id="cred-1")
    second = _credential(credential_id="cred-2")
    assert first.owner_id == second.owner_id
    assert first.credential_id != second.credential_id
    assert not hasattr(first, "parent_credential_id")
    assert not hasattr(first, "depth")
