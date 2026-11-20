"""Tests for astralplane.repositories.guidance_models and selected_input_models:
selected-input envelope round-trips detach private bindings, and closed
identity/limit domains reject unordered or mismatched snapshots.
"""

from dataclasses import asdict, replace
from uuid import uuid4

import pytest

from astralplane.repositories import RepositoryValidationError
from astralplane.repositories.guidance_models import GuidanceReference
from astralplane.repositories.selected_input_models import (
    AssignmentSelectedInput,
    SelectedAgentReference,
    SelectedInputEnvelope,
    copy_envelope,
    decode_envelope,
)


def envelope(**changes):
    values = dict(
        references=(),
        agent=SelectedAgentReference("research", str(uuid4()), "a" * 64),
        binding_key_id="private_1",
        combined_binding="b" * 64,
    )
    values.update(changes)
    return SelectedInputEnvelope(**values)


def test_exact_roundtrip_detaches_and_does_not_repr_private_bindings():
    ref = GuidanceReference("skill", str(uuid4()), 1)
    agent = SelectedAgentReference("rêsearch", str(uuid4()), "a" * 64)
    value = envelope(references=(ref,), agent=agent)
    detached = copy_envelope(value)
    object.__setattr__(ref, "revision", 2)
    object.__setattr__(agent, "agent_id", "changed")
    assert value == detached and value.references[0].revision == 1
    raw = asdict(value)
    raw["references"] = list(raw["references"])
    assert decode_envelope(raw) == value
    assert "private_1" not in repr(value) and "b" * 64 not in repr(value)
    snapshot = AssignmentSelectedInput("owner", str(uuid4()), 1, value, value.references)
    assert snapshot.envelope == value


@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"version": 2},
        {"expansion_version": True},
        {"binding_key_id": "PRIVATE"},
        {"binding_key_id": "a" * 33},
        {"combined_binding": "A" * 64},
        {"combined_binding": 1},
        {"references": []},
        {"references": ({},)},
        {"agent": {}},
        {"agent": None},
        {"references": tuple(GuidanceReference("skill", str(uuid4()), 1) for _ in range(21))},
        {"references": tuple(GuidanceReference("note", str(uuid4()), 1) for _ in range(9))},
    ],
)
def test_closed_envelope_refuses(changes):
    with pytest.raises(RepositoryValidationError):
        envelope(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"agent_id": " x"},
        {"agent_id": "x\n"},
        {"agent_id": "x" * 256},
        {"kind": "executable"},
        {"kind": True},
        {"revision_id": str(uuid4()).upper()},
        {"definition_digest": "f" * 63},
    ],
)
def test_agent_identity_refuses(changes):
    with pytest.raises(RepositoryValidationError):
        replace(envelope().agent, **changes)


def test_limits_canonical_order_duplicates_and_snapshot_mismatch():
    refs = tuple(GuidanceReference("skill", str(uuid4()), 1) for _ in range(20))
    refs += tuple(GuidanceReference("note", str(uuid4()), 2**53 - 1) for _ in range(8))
    value = envelope(references=refs)
    assert value.references == tuple(sorted(refs, key=lambda x: (x.kind, x.resource_id)))
    with pytest.raises(RepositoryValidationError):
        envelope(references=(refs[0], refs[0]))
    with pytest.raises(RepositoryValidationError):
        AssignmentSelectedInput("owner", str(uuid4()), 1, value, ())
    with pytest.raises(RepositoryValidationError):
        copy_envelope({})


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(extra="private body"),
        lambda d: d.pop("version"),
        lambda d: d.update(references={}),
        lambda d: d.update(references=[{"kind": "skill"}]),
        lambda d: d["agent"].update(extra="private text"),
        lambda d: d.update(agent=[]),
    ],
)
def test_persisted_shape_refuses(mutation):
    raw = asdict(envelope())
    raw["references"] = []
    mutation(raw)
    with pytest.raises(RepositoryValidationError):
        decode_envelope(raw)


def test_persisted_order_is_not_silently_normalized():
    refs = tuple(
        sorted(
            (GuidanceReference("skill", str(uuid4()), 1) for _ in range(2)),
            key=lambda r: r.resource_id,
        )
    )
    raw = asdict(envelope(references=refs))
    raw["references"] = list(reversed(raw["references"]))
    with pytest.raises(RepositoryValidationError):
        decode_envelope(raw)
