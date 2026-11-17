"""Tests for src/astralplane/domain.py: owner-context/receipt normalization, identifier
and UTC validation, and depth/canonicalization limits on frozen domain values.
"""

from __future__ import annotations

from collections import UserDict
from datetime import UTC, datetime, timedelta

import pytest

from astralplane.domain import (
    MutationReceipt,
    MutationState,
    OwnerContext,
    VersionedIdentity,
    freeze_domain_value,
    require_identifier,
    require_sha256,
    require_utc,
)
from astralplane.errors import DomainValidationError


def test_owner_context_and_receipt_are_normalized_and_detached() -> None:
    context = OwnerContext("owner-1", "actor-1", "corr/1", "tenant:1")
    source = {"nested": ["value", 1, True, None], "count": 2.5}
    receipt = MutationReceipt(
        VersionedIdentity(context.owner_id, "record-1", 3),
        MutationState.APPLIED,
        "operation-1",
        datetime(2026, 8, 13, 20, 0, tzinfo=UTC),
        source,
    )
    source["nested"].append("later")  # type: ignore[union-attr]

    assert receipt.committed_at.tzinfo is UTC
    assert receipt.metadata["nested"] == ("value", 1, True, None)
    with pytest.raises(TypeError):
        receipt.metadata["new"] = "denied"  # type: ignore[index]


@pytest.mark.parametrize(
    ("call", "value"),
    [
        (lambda value: require_identifier(value, field="id"), " has-space"),
        (lambda value: require_identifier(value, field="id"), ""),
        (lambda value: require_identifier(value, field="id"), "a" * 129),
        (require_sha256, "A" * 64),
        (require_sha256, "0" * 63),
    ],
)
def test_identifier_and_digest_validation_fail_closed(call: object, value: str) -> None:
    with pytest.raises(DomainValidationError):
        call(value)  # type: ignore[operator]


def test_utc_validation_normalizes_offsets_and_rejects_naive_values() -> None:
    observed = require_utc(
        datetime(2026, 8, 13, 16, 0, tzinfo=UTC) + timedelta(),
        field="time",
    )
    assert observed.tzinfo is UTC
    with pytest.raises(DomainValidationError):
        require_utc(datetime(2026, 8, 13), field="time")


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        "nul\x00text",
        "x" * 16_385,
        {1: "bad"},
        UserDict({"key": "value"}),
        {"bad": object()},
        ("nested",) * 4097,
    ],
)
def test_freeze_domain_value_rejects_noncanonical_or_unbounded_values(value: object) -> None:
    with pytest.raises(DomainValidationError):
        freeze_domain_value(value)


def test_freeze_domain_value_rejects_excessive_depth() -> None:
    value: object = "leaf"
    for _ in range(14):
        value = [value]
    with pytest.raises(DomainValidationError, match="nesting"):
        freeze_domain_value(value)


@pytest.mark.parametrize(
    "builder",
    [
        lambda: OwnerContext("bad id", "actor", "corr"),
        lambda: OwnerContext("owner", "actor", "corr", "bad tenant"),
        lambda: VersionedIdentity("owner", "record", -1),
        lambda: VersionedIdentity("owner", "record", True),
        lambda: MutationReceipt(
            VersionedIdentity("owner", "record", 0),
            MutationState.APPLIED,
            "bad operation",
            datetime.now(UTC),
            {},
        ),
    ],
)
def test_invalid_domain_records_are_refused(builder: object) -> None:
    with pytest.raises(DomainValidationError):
        builder()  # type: ignore[operator]
