"""Closed manual-event and owner-attestation request vocabulary."""
from uuid import uuid4

import pytest
from pydantic import ValidationError

from orchestrator.work_continuations import WorkOwnerWaitRequest, WorkReconcileRequest


def wait_fields():
    return {"submission_id": str(uuid4()), "expected_revision": 1,
            "owner_event_id": str(uuid4()), "owner_revision": 0}


def reconcile_fields():
    return {"submission_id": str(uuid4()), "expected_revision": 1,
            "prior_result_digest": "a" * 64, "decision": "confirmed_applied"}


@pytest.mark.parametrize("field,value", [
    ("owner_revision", True), ("owner_revision", "1"), ("owner_revision", 1.0),
    ("owner_revision", -1), ("owner_revision", 2**53),
    ("owner_event_id", "provider:event"), ("owner_event_id", str(uuid4()).upper()),
    ("owner_event_id", "00000000-0000-1000-8000-000000000000"),
    ("owner_event_id", None), ("owner_event_id", {}),
    ("event_key", "source:changed"), ("source_revision", 4),
    ("payload", {"private": "source"}), ("evidence_reference", "https://example.org"),
])
def test_owner_wait_never_accepts_provider_evidence_or_coerced_identity(field, value):
    fields = wait_fields()
    fields[field] = value
    with pytest.raises(ValidationError):
        WorkOwnerWaitRequest.model_validate(fields)


@pytest.mark.parametrize("field,value", [
    ("prior_result_digest", "A" * 64), ("prior_result_digest", "a" * 63),
    ("prior_result_digest", "a" * 64 + "\n"), ("prior_result_digest", True),
    ("decision", "retry"), ("decision", "refund"), ("decision", True),
    ("decision", {"confirmed_applied": True}), ("evidence_reference", "provider:receipt"),
    ("result", {"answer": "private"}), ("authority", {"session": "foreign"}),
    ("dispatch_token", "private"), ("actual", {"tool_calls": 0}),
])
def test_reconciliation_accepts_only_an_exact_owner_attestation(field, value):
    fields = reconcile_fields()
    fields[field] = value
    with pytest.raises(ValidationError):
        WorkReconcileRequest.model_validate(fields)


def test_closed_request_boundaries_preserve_canonical_owner_values():
    wait = WorkOwnerWaitRequest(**{**wait_fields(), "owner_revision": 2**53 - 1})
    assert wait.owner_revision == 2**53 - 1
    for decision in ("confirmed_applied", "confirmed_not_applied"):
        value = WorkReconcileRequest(**{**reconcile_fields(), "decision": decision})
        assert value.decision == decision and value.prior_result_digest == "a" * 64
