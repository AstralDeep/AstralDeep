"""Tests for astralplane.repositories.assignments, result_publication_models,
result_publications, and workspaces: proposal shape, canvas/layout domain,
content-size limits, and identity counters validate before any database access.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from astralplane.repositories import RepositoryConflictError, RepositoryValidationError
from astralplane.repositories.assignments import (
    AssignmentActionDecision,
    AssignmentRepository,
    digest,
)
from astralplane.repositories.result_publication_models import (
    ResultPublicationContent,
    ResultPublicationProposal,
)
from astralplane.repositories.result_publications import result_publication_stage_digest
from astralplane.repositories.workspaces import PublicationRebaseComponent, PublicationRebaseLayout

ID = "36da3fbb-89dc-4bc3-a12d-ea71b105b33e"
SECOND = "9d18c859-10d7-4709-bc0e-c9ffbd77f236"


def content():
    return ResultPublicationContent(
        (
            PublicationRebaseComponent(
                ID, "result", {"type": "text", "text": "literal"}, "text", "Public", 0
            ),
        )
    )


def proposal():
    value = content()
    return ResultPublicationProposal(
        ID,
        SECOND,
        digest("model"),
        SECOND,
        "chat",
        "result",
        0,
        None,
        digest(value.components[0].payload),
        result_publication_stage_digest(value),
        digest("permission"),
        digest("precondition"),
        datetime.now(UTC) + timedelta(minutes=1),
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("action_id", "bad"),
        ("result_action_id", ID),
        ("conversation_id", 1),
        ("component_id", ""),
        ("result_digest", "bad"),
        ("base_render_revision", True),
        ("base_render_revision", -1),
        ("base_render_revision", 1.0),
        ("base_publication_id", "unexpected"),
        ("version", True),
        ("version", 2),
        ("expires_at", None),
        ("expires_at", datetime(2026, 1, 1)),
        ("expires_at", "later"),
    ],
)
def test_proposal_shape_before_database(field, value):
    with pytest.raises((RepositoryValidationError, RepositoryConflictError)):
        AssignmentRepository().put_result_publication_proposal(
            object(),
            owner_id="owner",
            assignment_id=SECOND,
            expected_instruction_revision=1,
            expected_control_epoch=1,
            expected_state_version=1,
            proposal=replace(proposal(), **{field: value}),
            content=content(),
            expected_selected=None,
            authority=None,
            caller_valid_until=None,
        )


@pytest.mark.parametrize(
    "change",
    [
        "wrong_type",
        "empty",
        "count",
        "component_type",
        "title",
        "position",
        "duplicate",
        "duplicate_row",
        "layout_type",
        "layout_position",
        "nonfinite",
    ],
)
def test_complete_canvas_and_layout_domain_before_database(change):
    value = content()
    entry = value.components[0]
    if change == "wrong_type":
        value = {"components": []}
    if change == "empty":
        value = ResultPublicationContent(())
    if change == "count":
        value = ResultPublicationContent((entry,) * 10001)
    if change == "component_type":
        value = replace(value, components=(replace(entry, component_type=True),))
    if change == "title":
        value = replace(value, components=(replace(entry, title=1),))
    if change == "position":
        value = replace(value, components=(replace(entry, position=3),))
    if change == "duplicate":
        value = replace(value, components=(entry, entry))
    if change == "duplicate_row":
        value = replace(value, components=(entry, replace(entry, component_id="other", position=1)))
    if change == "layout_type":
        value = replace(value, layouts=(PublicationRebaseLayout(1, 0, {}),))
    if change == "layout_position":
        value = replace(value, layouts=(PublicationRebaseLayout("main", True, {}),))
    if change == "nonfinite":
        value = replace(value, components=(replace(entry, payload={"value": float("nan")}),))
    with pytest.raises(RepositoryValidationError):
        result_publication_stage_digest(value)


@pytest.mark.parametrize("kind", ["components", "layouts"])
def test_aggregate_content_limit_stops_before_reading_later_payloads(kind):
    payload = {"text": "x" * 250_000}
    if kind == "components":
        entries = tuple(
            PublicationRebaseComponent(str(i), str(i), payload, "text", None, i)
            for i in range(34)
        )
        value = ResultPublicationContent(entries)
        poisoned = replace(
            value,
            components=(
                *entries,
                PublicationRebaseComponent("later", "later", object(), "text", None, 34),
            ),
        )
    else:
        entries = tuple(PublicationRebaseLayout(str(i), i, payload) for i in range(34))
        value = replace(content(), layouts=entries)
        poisoned = replace(
            value, layouts=(*entries, PublicationRebaseLayout("later", 34, object()))
        )
    for candidate in (value, poisoned):
        with pytest.raises(RepositoryValidationError, match="JSON exceeds its bound"):
            result_publication_stage_digest(candidate)


@pytest.mark.parametrize(
    "field,value",
    [
        ("decision", "decline"),
        ("decision", True),
        ("submission_id", "bad"),
        ("proposal_digest", "bad"),
    ],
)
def test_decision_is_closed_before_database(field, value):
    decision = AssignmentActionDecision(
        digest("proposal"),
        "approve",
        ID,
        digest("save"),
        digest("permission"),
        digest("precondition"),
    )
    with pytest.raises(RepositoryValidationError):
        AssignmentRepository().prepare_result_publication(
            object(),
            owner_id="owner",
            assignment_id=SECOND,
            action_id=ID,
            expected_state_version=1,
            decision=replace(decision, **{field: value}),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_id", True),
        ("assignment_id", "bad"),
        ("action_id", "bad"),
        ("expected_state_version", True),
        ("expected_state_version", 0),
    ],
)
def test_original_identity_and_counters_are_exact_before_database(field, value):
    args = dict(
        owner_id="owner",
        assignment_id=SECOND,
        action_id=ID,
        expected_state_version=1,
        decision=AssignmentActionDecision(
            digest("proposal"),
            "approve",
            ID,
            digest("save"),
            digest("permission"),
            digest("precondition"),
        ),
    )
    with pytest.raises(RepositoryValidationError):
        AssignmentRepository().prepare_result_publication(object(), **dict(args, **{field: value}))
