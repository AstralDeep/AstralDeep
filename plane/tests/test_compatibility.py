"""Tests for src/astralplane/compatibility.py: producer-side schema compatibility
metadata and deterministic, stably-ordered mismatch reason codes.
"""

from __future__ import annotations

import pytest

from astralplane.compatibility import (
    BLOB_LAYOUT_VERSION,
    CONTRACT_VERSION,
    MIGRATION_DIGEST,
    PACKAGE_VERSION,
    RECOVERY_CONTRACT_VERSION,
    CompatibilityState,
    inspect_compatibility,
)


def test_current_and_predecessor_schema_are_compatible() -> None:
    for revision in (
        "066.001",
        "067.001",
        "074.001",
        "074.002",
        "074.003",
        "074.004",
    ):
        report = inspect_compatibility(
            expected_contract_version=CONTRACT_VERSION,
            observed_schema_revision=revision,
            consumer_version=PACKAGE_VERSION,
        )
        assert report.compatible
        assert report.state is CompatibilityState.COMPATIBLE
        assert report.reasons == ()
        payload = report.to_dict()
        assert payload["migration_digest"] == MIGRATION_DIGEST
        assert payload["blob_layout_version"] == BLOB_LAYOUT_VERSION
        assert payload["recovery_contract_version"] == RECOVERY_CONTRACT_VERSION
        assert payload["compatible"] is True


@pytest.mark.parametrize(
    ("contract", "schema", "consumer", "reason"),
    [
        ("astralplane.contract/v2", "067.001", "0.1.0", "contract_version_mismatch"),
        (CONTRACT_VERSION, "065.001", "0.1.0", "schema_revision_incompatible"),
        (CONTRACT_VERSION, "bad", "0.1.0", "schema_revision_incompatible"),
        (CONTRACT_VERSION, "067.001", "0.0.9", "consumer_version_too_old"),
        (CONTRACT_VERSION, "067.001", "v0.1.0", "consumer_version_too_old"),
    ],
)
def test_incompatible_compositions_report_stable_reason_codes(
    contract: str,
    schema: str,
    consumer: str,
    reason: str,
) -> None:
    report = inspect_compatibility(
        expected_contract_version=contract,
        observed_schema_revision=schema,
        consumer_version=consumer,
    )
    assert not report.compatible
    assert report.state is CompatibilityState.INCOMPATIBLE
    assert reason in report.reasons


def test_multiple_mismatches_are_reported_in_deterministic_order() -> None:
    report = inspect_compatibility(
        expected_contract_version="wrong",
        observed_schema_revision="000.000",
        consumer_version="bad",
    )
    assert report.reasons == (
        "contract_version_mismatch",
        "schema_revision_incompatible",
        "consumer_version_too_old",
    )
