"""Typed harness admission captures reject ambiguous JSON and retain detached identities.
These records are consumed by preference, assignment and work-admission facades.
"""

from dataclasses import FrozenInstanceError, replace

import pytest

from astralplane.repositories import RepositoryValidationError
from astralplane.repositories.harness_bindings import HarnessProfileIdentity
from astralplane.repositories.harness_capture import (
    HarnessCapture,
    HarnessSelectionRecord,
    harness_capture_from_value,
)


def capture(**changes):
    values = dict(
        owner_id="owner-a",
        selection_revision=1,
        identity=HarnessProfileIdentity("standard_local", "1.0.0", "a" * 64),
        qualification_digest="b" * 64,
        research_profile="local-page-selection/v1",
        config_binding_key_id="active-key",
        config_revision="c" * 64,
    )
    values.update(changes)
    return HarnessCapture(**values)


def test_capture_roundtrip_is_exact_detached_and_immutable():
    value = capture()
    document = value.to_dict()
    assert harness_capture_from_value(document) == value
    document["identity"]["profile"] = "changed"
    assert value.identity.profile == "standard_local"
    with pytest.raises(FrozenInstanceError):
        value.config_revision = "d" * 64
    assert "owner-a" not in repr(value) and "active-key" not in repr(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_id": ""},
        {"owner_id": "a" * 513},
        {"owner_id": "a\n"},
        {"owner_id": True},
        {"selection_revision": True},
        {"selection_revision": 0},
        {"selection_revision": 2**53},
        {"identity": {}},
        {"qualification_digest": "x"},
        {"research_profile": "other"},
        {"config_binding_key_id": ""},
        {"config_binding_key_id": "a\x00"},
        {"config_binding_key_id": "a" * 129},
        {"config_revision": "C" * 64},
        {"version": True},
        {"version": 2},
    ],
)
def test_capture_fields_are_strict(changes):
    with pytest.raises(RepositoryValidationError):
        capture(**changes)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(extra="no"),
        lambda d: d.pop("owner_id"),
        lambda d: d.pop("version"),
        lambda d: d["identity"].update(extra="no"),
        lambda d: d["identity"].update(version=True),
        lambda d: d.update(identity=[]),
        lambda d: d.update(owner_id=None),
    ],
)
def test_capture_decoder_rejects_unknown_missing_or_malformed_fields(mutation):
    value = capture().to_dict()
    mutation(value)
    with pytest.raises(RepositoryValidationError):
        harness_capture_from_value(value)


@pytest.mark.parametrize("value", [None, [], "{}", True, {"x": "a" * 4097}])
def test_capture_decoder_rejects_nonobjects_and_oversized_values(value):
    with pytest.raises(RepositoryValidationError):
        harness_capture_from_value(value)


def test_selection_null_and_nonnull_identity_are_consistent():
    empty = HarnessSelectionRecord("owner-a", 0, None, None)
    assert empty.identity is None
    selected = HarnessSelectionRecord("owner-a", 1, capture().identity, "b" * 64)
    assert selected.revision == 1
    for value in (True, -1, 2**53):
        with pytest.raises(RepositoryValidationError):
            replace(empty, revision=value)
    for changes in (
        {"identity": capture().identity},
        {"qualification_digest": "b" * 64},
        {"owner_id": ""},
    ):
        with pytest.raises(RepositoryValidationError):
            replace(empty, **changes)
    with pytest.raises(RepositoryValidationError):
        replace(selected, revision=0)


def test_capture_encoded_json_bound_counts_multibyte_owner_without_retaining_content():
    with pytest.raises(RepositoryValidationError):
        capture(owner_id="🟢" * 512)
    value = capture().to_dict()
    value["owner_id"] = "🟢" * 512
    with pytest.raises(RepositoryValidationError):
        harness_capture_from_value(value)
