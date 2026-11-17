"""Tests for astralplane.authority.claims: receipt-claim, watermark, and external-anchor
field exactness, UTC normalization, and fail-closed identifier validation.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

import astralplane.authority as authority
from astralplane.authority.claims import (
    EXECUTOR_ANCHOR_FORMAT,
    ExternalAuthorityAnchorMetadata,
    ReceiptClaim,
    ReceiptSequenceWatermark,
)
from astralplane.errors import DomainValidationError

POLICY_DIGEST = "sha256:" + "1" * 64
MACHINE_DIGEST = "sha256:" + "2" * 64
EVIDENCE_DIGEST = "sha256:" + "3" * 64
CANONICAL_DIGEST = "4" * 64
EXECUTOR_POLICY_SHA256 = "5" * 64
TRUST_REGISTRY_SHA256 = "6" * 64
DATABASE_INSTANCE_ID = "7" * 64
CLAIM_DIGEST = "8" * 64
NOW = datetime(2026, 8, 14, 18, tzinfo=UTC)


def test_authority_namespace_exports_effect_and_claim_records() -> None:
    assert authority.ProtectedEffectOperation.__name__ == "ProtectedEffectOperation"
    assert authority.ReceiptClaim is ReceiptClaim
    assert authority.EXECUTOR_ANCHOR_FORMAT == EXECUTOR_ANCHOR_FORMAT


def _anchor(**changes: object) -> ExternalAuthorityAnchorMetadata:
    values: dict[str, object] = {
        "anchor_format": EXECUTOR_ANCHOR_FORMAT,
        "audience": "executor-a",
        "tenant_id": "tenant-1",
        "envelope_id": "envelope-1",
        "config_epoch": 7,
        "executor_policy_sha256": EXECUTOR_POLICY_SHA256,
        "trust_registry_sha256": TRUST_REGISTRY_SHA256,
        "schema_version": 5,
        "database_instance_id": DATABASE_INSTANCE_ID,
        "claim_sequence": 12,
        "claim_digest": CLAIM_DIGEST,
        "clock_floor_ns": 1_723_658_400_000_000_000,
        "confirmed_at": NOW + timedelta(milliseconds=1),
    }
    values.update(changes)
    return ExternalAuthorityAnchorMetadata(**values)  # type: ignore[arg-type]


def _claim(**changes: object) -> ReceiptClaim:
    values: dict[str, object] = {
        "receipt_id": "receipt-1",
        "operation_id": "operation-1",
        "owner_id": "owner-1",
        "binding_id": "binding-1",
        "tenant_id": "tenant-1",
        "envelope_id": "envelope-1",
        "warden_id": "warden-1",
        "lease_id": "lease-1",
        "subject_id": "agent-1",
        "lineage_id": "lineage-1",
        "policy_digest": POLICY_DIGEST,
        "machine_digest": MACHINE_DIGEST,
        "config_epoch": 7,
        "audience": "executor-a",
        "transition": "tool_read",
        "nonce": "nonce-1",
        "resulting_sequence": 11,
        "evidence_digest": EVIDENCE_DIGEST,
        "issued_at_ns": 1_723_658_399_000_000_000,
        "expires_at_ns": 1_723_658_430_000_000_000,
        "claimed_at": NOW,
        "canonical_digest": CANONICAL_DIGEST,
        "authority_anchor": _anchor(),
    }
    values.update(changes)
    return ReceiptClaim(**values)  # type: ignore[arg-type]


def _watermark(**changes: object) -> ReceiptSequenceWatermark:
    values: dict[str, object] = {
        "warden_id": "warden-1",
        "lease_id": "lease-1",
        "audience": "executor-a",
        "last_sequence": 10,
        "updated_at": NOW,
        "expires_at_ns": 1_723_658_430_000_000_000,
        "version": 2,
    }
    values.update(changes)
    return ReceiptSequenceWatermark(**values)  # type: ignore[arg-type]


def test_claim_exposes_exact_database_uniqueness_and_watermark_keys() -> None:
    claim = _claim()

    assert claim.receipt_uniqueness_key == "receipt-1"
    assert claim.nonce_uniqueness_key == (
        "tenant-1",
        "envelope-1",
        "executor-a",
        "nonce-1",
    )
    assert claim.sequence_watermark_key == ("warden-1", "lease-1", "executor-a")
    assert claim.owner_operation_key == ("owner-1", "operation-1")
    with pytest.raises(FrozenInstanceError):
        claim.receipt_id = "receipt-2"  # type: ignore[misc]


def test_watermark_requires_same_domain_and_strictly_advancing_sequence() -> None:
    watermark = _watermark()
    watermark.require_advance(key=watermark.key, sequence=11)

    with pytest.raises(DomainValidationError, match="strictly advance"):
        watermark.require_advance(key=watermark.key, sequence=10)
    with pytest.raises(DomainValidationError, match="strictly advance"):
        watermark.require_advance(key=watermark.key, sequence=9)
    with pytest.raises(DomainValidationError, match="does not match"):
        watermark.require_advance(
            key=("warden-2", "lease-1", "executor-a"),
            sequence=11,
        )


def test_anchor_retains_stable_identity_and_monotonic_head() -> None:
    anchor = _anchor()

    assert anchor.stable_identity == (
        "executor-a",
        "tenant-1",
        "envelope-1",
        7,
        EXECUTOR_POLICY_SHA256,
        TRUST_REGISTRY_SHA256,
        5,
        DATABASE_INSTANCE_ID,
    )
    assert anchor.head == (12, CLAIM_DIGEST)


def test_anchor_and_claim_timestamps_normalize_to_utc() -> None:
    offset = timezone(timedelta(hours=-4))
    claim = _claim(
        claimed_at=datetime(2026, 8, 14, 14, tzinfo=offset),
        authority_anchor=_anchor(
            confirmed_at=datetime(2026, 8, 14, 14, 0, 0, 1_000, tzinfo=offset)
        ),
    )

    assert claim.claimed_at == NOW
    assert claim.authority_anchor.confirmed_at == NOW + timedelta(milliseconds=1)


@pytest.mark.parametrize(
    "field",
    [
        "receipt_id",
        "operation_id",
        "owner_id",
        "binding_id",
        "tenant_id",
        "envelope_id",
        "warden_id",
        "lease_id",
        "subject_id",
        "lineage_id",
        "audience",
        "transition",
        "nonce",
    ],
)
def test_claim_identifiers_fail_closed_without_echoing_values(field: str) -> None:
    invalid = " secret value "
    with pytest.raises(DomainValidationError) as caught:
        _claim(**{field: invalid})
    assert invalid not in str(caught.value)


@pytest.mark.parametrize("field", ["policy_digest", "machine_digest", "evidence_digest"])
@pytest.mark.parametrize("value", ["1" * 64, "sha256:" + "A" * 64, "sha256:short"])
def test_claim_lets_digests_require_wire_format(field: str, value: str) -> None:
    with pytest.raises(DomainValidationError, match="canonical LETS SHA-256"):
        _claim(**{field: value})


def test_optional_evidence_digest_can_be_absent() -> None:
    assert _claim(evidence_digest=None).evidence_digest is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"config_epoch": 0}, "config epoch"),
        ({"resulting_sequence": 0}, "resulting sequence"),
        ({"issued_at_ns": -1}, "issued at"),
        ({"expires_at_ns": 0}, "expires at"),
        ({"expires_at_ns": 1_723_658_399_000_000_000}, "expiry must follow"),
        ({"canonical_digest": "sha256:" + "4" * 64}, "canonical receipt digest"),
        ({"claimed_at": datetime(2026, 8, 14, 18)}, "timezone-aware"),
        ({"authority_anchor": object()}, "anchor metadata"),
    ],
)
def test_claim_structural_fences_fail_closed(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(DomainValidationError, match=message):
        _claim(**changes)


@pytest.mark.parametrize(
    ("claim_change", "anchor_change"),
    [
        ({"audience": "executor-b"}, {}),
        ({"tenant_id": "tenant-2"}, {}),
        ({"envelope_id": "envelope-2"}, {}),
        ({"config_epoch": 8}, {}),
        ({}, {"audience": "executor-b"}),
        ({}, {"tenant_id": "tenant-2"}),
        ({}, {"envelope_id": "envelope-2"}),
        ({}, {"config_epoch": 8}),
    ],
)
def test_claim_requires_exact_external_anchor_identity(
    claim_change: dict[str, object],
    anchor_change: dict[str, object],
) -> None:
    with pytest.raises(DomainValidationError, match="identity does not match"):
        _claim(**claim_change, authority_anchor=_anchor(**anchor_change))


@pytest.mark.parametrize(
    ("anchor", "message"),
    [
        (
            _anchor(claim_sequence=0, claim_digest="0" * 64, clock_floor_ns=None),
            "nonempty authority anchor head",
        ),
        (_anchor(clock_floor_ns=None), "anchored clock floor"),
        (
            _anchor(confirmed_at=NOW - timedelta(microseconds=1)),
            "confirmation cannot precede",
        ),
    ],
)
def test_claim_requires_post_claim_external_authority_confirmation(
    anchor: ExternalAuthorityAnchorMetadata,
    message: str,
) -> None:
    with pytest.raises(DomainValidationError, match=message):
        _claim(authority_anchor=anchor)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"anchor_format": "LETS-EXECUTOR-AUTHORITY-ANCHOR/2"}, "format"),
        ({"config_epoch": 0}, "config epoch"),
        ({"schema_version": 0}, "schema version"),
        ({"claim_sequence": -1}, "claim sequence"),
        ({"clock_floor_ns": -1}, "clock floor"),
        ({"confirmed_at": datetime(2026, 8, 14, 18)}, "timezone-aware"),
    ],
)
def test_anchor_structural_fences_fail_closed(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(DomainValidationError, match=message):
        _anchor(**changes)


@pytest.mark.parametrize(
    "field",
    [
        "executor_policy_sha256",
        "trust_registry_sha256",
        "database_instance_id",
        "claim_digest",
    ],
)
def test_anchor_binary_values_use_plane_hex_normalization(field: str) -> None:
    with pytest.raises(DomainValidationError, match="lowercase SHA-256"):
        _anchor(**{field: "sha256:" + "a" * 64})


def test_empty_anchor_head_uses_only_the_zero_digest() -> None:
    empty = _anchor(claim_sequence=0, claim_digest="0" * 64, clock_floor_ns=None)
    assert empty.head == (0, "0" * 64)

    with pytest.raises(DomainValidationError, match="zero digest"):
        _anchor(claim_sequence=0)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"last_sequence": 0}, "watermark sequence"),
        ({"expires_at_ns": 0}, "watermark expiry"),
        ({"version": -1}, "watermark version"),
        ({"version": False}, "watermark version"),
        ({"updated_at": datetime(2026, 8, 14, 18)}, "timezone-aware"),
    ],
)
def test_watermark_structural_fences_fail_closed(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(DomainValidationError, match=message):
        _watermark(**changes)


def test_replace_preserves_claim_and_watermark_validation() -> None:
    claim = replace(_claim(), resulting_sequence=13)
    watermark = replace(_watermark(), last_sequence=12, version=3)

    watermark.require_advance(key=claim.sequence_watermark_key, sequence=claim.resulting_sequence)
    assert claim.resulting_sequence == 13
    assert watermark.version == 3
