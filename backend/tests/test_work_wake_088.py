"""Closed manual-owner wake request semantics; no route or dispatch exposure."""

import importlib
from uuid import uuid4

import pytest
from pydantic import ValidationError


def module():
    return importlib.import_module("orchestrator.work_wake")


def values():
    return {
        "expected_revision": 1,
        "submission_id": str(uuid4()),
        "owner_event_id": str(uuid4()),
        "owner_revision": 1,
    }


def test_closed_manual_owner_request_contract():
    fields = values()
    assert module().WorkOwnerWakeRequest(**fields).model_dump() == fields


@pytest.mark.parametrize("field,value", [
    ("expected_revision", True), ("expected_revision", 0),
    ("owner_revision", True), ("owner_revision", -1),
    ("owner_revision", 2**53), ("owner_event_id", "provider-event"),
    ("owner_event_id", str(uuid4()).upper()), ("submission_id", "caller-key"),
    ("event_digest", "0" * 64), ("source_revision", 1),
    ("event_key", "provider:remote"), ("authority", {}),
])
def test_request_refuses_untrusted_event_or_authority_fields(field, value):
    with pytest.raises(ValidationError):
        module().WorkOwnerWakeRequest(**{**values(), field: value})


def test_request_allows_full_safe_integer_owner_revision():
    assert module().WorkOwnerWakeRequest(**{
        **values(), "owner_revision": 2**53 - 1,
    }).owner_revision == 2**53 - 1
