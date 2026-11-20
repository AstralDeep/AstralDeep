"""Real-PostgreSQL tests for astralplane.repositories.assignments, history,
result_publications, and workspaces: a completed Save is one internal transaction,
replay is read-only, and a new save can never adopt lost original authority.
"""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_assignments_postgres import (
    action,
    create_operation,
    definition,
    finish,
    reserve,
    session_observation,
    start,
    uid,
)
from test_assignments_postgres import database as database
from test_assignments_postgres import repo as repo
from test_assignments_postgres import tx as tx
from test_operation_control_postgres import current, operation_claim
from test_operation_payload_postgres import admission, payload, settle_args, transient_action
from test_operation_terminal_postgres import expire_authority

from astralplane.repositories import RepositoryConflictError, RepositoryValidationError
from astralplane.repositories.assignments import (
    AssignmentActionDecision,
    AssignmentActionOutcome,
    AssignmentInputReference,
    AssignmentOperationAuthority,
    AssignmentOperationSpec,
    ResultPublicationContent,
    ResultPublicationProposal,
    canonical,
    digest,
)
from astralplane.repositories.history import ConversationRepository, SessionRepository
from astralplane.repositories.result_publications import result_publication_stage_digest
from astralplane.repositories.workspaces import (
    CanvasRepository,
    PublicationRebaseComponent,
    PublicationRebaseLayout,
    PublicationRepository,
)


def completed(repo, tx, *, select=None):
    observed = session_observation(tx)
    now = observed.started_at
    base = definition(
        tx,
        source={
            "agent_id": "web-research-1",
            "tool_name": "fetch_page",
            "arguments": {"url": "https://example.org"},
        },
    )
    record = create_operation(
        repo,
        tx,
        definition=replace(
            base,
            offline_grant_id=None,
            limits={
                key: value
                for key, value in base.limits.items()
                if key != "cadence_seconds" and not key.startswith("daily_")
            },
        ),
        operation=AssignmentOperationSpec(
            "research",
            AssignmentOperationAuthority(
                "owner",
                "interactive",
                "session_incarnation",
                observed.credential.incarnation_id,
                now + timedelta(minutes=5),
            ),
            now + timedelta(minutes=5),
            "operation",
        ),
    )
    if select is not None:
        record = select(tx, record)
    claim = operation_claim(repo, tx)
    _, _, binding = admission(repo, tx, claim)
    source = action(
        repo,
        tx,
        claim.fence,
        request={"kind": "tool", **base.source},
        request_digest=digest({"kind": "tool", **base.source}),
    )
    permit = start(repo, tx, claim.fence, reserve(repo, tx, claim.fence, source), binding)
    repo.record_action_outcome(
        tx,
        **dict(
            settle_args(tx, record, claim, binding, permit),
            outcome=AssignmentActionOutcome(
                "succeeded", digest("public source"), {"text": "public source"}
            ),
        ),
    )
    selected = repo.get_selected_input(tx, owner_id="owner", assignment_id=record.assignment_id)
    refs = (
        ()
        if selected is None
        else tuple(
            AssignmentInputReference(ref.kind, ref.resource_id, ref.revision)
            for ref in selected.references
        )
    )
    key = "synthetic-key-v1" if selected is None else selected.envelope.binding_key_id
    model = transient_action(
        repo,
        tx,
        claim,
        transient_input=payload(
            source_retention="operation",
            binding_key_id=key,
            references=(AssignmentInputReference("source", source.action_id, 1), *refs),
        ),
    )
    permit = start(repo, tx, claim.fence, reserve(repo, tx, claim.fence, model), binding)
    from astralplane.repositories.assignments import AssignmentResultDisposition

    args = settle_args(tx, record, claim, binding, permit)
    args["outcome"] = replace(
        args["outcome"], result_disposition=AssignmentResultDisposition(True, binding_key_id=key)
    )
    repo.record_action_outcome(tx, **args)
    finish(repo, tx, claim.fence, completed=True, result_reference=model.action_id)
    return current(repo, tx, record), repo.get_action(
        tx, owner_id="owner", assignment_id=record.assignment_id, action_id=model.action_id
    )


def proposed(repo, tx, *, select=None, existing_head=False):
    record, model = completed(repo, tx, select=select)
    selected = repo.get_selected_input(tx, owner_id="owner", assignment_id=record.assignment_id)
    chat = uid()
    ConversationRepository().create(
        tx,
        conversation_id=chat,
        owner_id="owner",
        title="Synthetic Save",
        agent_id=None,
        created_at=1,
    )
    content = ResultPublicationContent(
        (
            PublicationRebaseComponent(
                uid(),
                "result",
                {"type": "text", "text": "Exact public result"},
                "text",
                "Public result",
                0,
            ),
        ),
        (PublicationRebaseLayout("main", 0, {"type": "column", "children": ["result"]}),),
    )
    now = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
    parent = None
    if existing_head:
        parent = uid()
        pubs = PublicationRepository()
        pubs.stage(
            tx,
            publication_id=parent,
            owner_id="owner",
            conversation_id=chat,
            request_generation=uid(),
            base_render_revision=0,
            started_at=now,
        )
        pubs.commit_at_head(
            tx,
            publication_id=parent,
            owner_id="owner",
            conversation_id=chat,
            expected_staged_base_render_revision=0,
            expected_head_render_revision=0,
            expected_head_publication_id=None,
            committed_at=now,
            updated_at=int(now.timestamp()),
        )
    proposal = ResultPublicationProposal(
        uid(),
        model.action_id,
        model.result["result_digest"],
        uid(),
        chat,
        "result",
        1 if existing_head else 0,
        parent,
        digest(content.components[0].payload),
        result_publication_stage_digest(content),
        digest("current permission"),
        digest("current precondition"),
        now + timedelta(minutes=1),
    )
    authority = session_observation(tx)
    create_args = dict(
        owner_id="owner",
        assignment_id=record.assignment_id,
        expected_instruction_revision=record.instruction_revision,
        expected_control_epoch=record.control_epoch,
        expected_state_version=record.state_version,
        proposal=proposal,
        content=content,
        expected_selected=selected,
        authority=authority,
        caller_valid_until=now + timedelta(minutes=1),
    )
    created = repo.put_result_publication_proposal(tx, **create_args)
    decision = AssignmentActionDecision(
        created.intent.request_digest,
        "approve",
        uid(),
        digest("save exact result"),
        proposal.permission_digest,
        proposal.precondition_digest,
    )
    after = current(repo, tx, record)
    args = dict(
        owner_id="owner",
        assignment_id=record.assignment_id,
        action_id=created.action_id,
        decision=decision,
        expected_state_version=after.state_version,
        authority=session_observation(tx),
        caller_valid_until=now + timedelta(minutes=1),
    )
    return after, proposal, content, args, create_args


def state(tx, record, proposal):
    return (
        tx.fetch_one("SELECT data FROM persistent_assignment WHERE id=%s", (record.assignment_id,)),
        tx.fetch_one(
            "SELECT data FROM persistent_assignment_action WHERE id=%s", (proposal.action_id,)
        ),
        ConversationRepository().get(
            tx, owner_id="owner", conversation_id=proposal.conversation_id
        ),
    )


def test_completed_result_save_is_one_internal_transaction_and_replay_is_read_only(tx, repo):
    record, proposal, content, args, creation = proposed(repo, tx)
    before = state(tx, record, proposal)
    prepared = repo.prepare_result_publication(tx, **args)
    assert not prepared.replayed and prepared.receipt is None
    assert state(tx, record, proposal) == before
    assert repo.put_result_publication_proposal(tx, **creation).action_id == proposal.action_id
    receipt = repo.commit_result_publication(tx, **args, content=content)
    accepted = state(tx, record, proposal)
    assert accepted[2].publication_id == proposal.publication_id
    assert accepted[0]["data"]["usage"] == before[0]["data"]["usage"]
    assert accepted[0]["data"]["lifecycle"] == "completed"
    assert accepted[1]["data"]["attempts"] == ()
    assert accepted[1]["data"]["state"] == "succeeded"
    assert repo._known_action(tx, "owner", record.assignment_id, proposal.action_id) is not None
    replay = repo.prepare_result_publication(
        tx, **dict(args, authority=None, caller_valid_until=None)
    )
    assert replay.replayed and replay.receipt == receipt
    assert repo.commit_result_publication(tx, **args, content=content) == receipt
    assert state(tx, record, proposal) == accepted


def test_new_save_can_publish_against_exact_existing_head(tx, repo):
    _record, proposal, content, args, _ = proposed(repo, tx, existing_head=True)
    receipt = repo.commit_result_publication(tx, **args, content=content)
    assert receipt.committed_render_revision == 2
    assert (
        ConversationRepository()
        .get(tx, owner_id="owner", conversation_id=proposal.conversation_id)
        .publication_id
        == proposal.publication_id
    )


def test_accepted_receipt_survives_original_retirement_and_later_workspace_head(tx, repo):
    record, proposal, content, args, _ = proposed(repo, tx)
    receipt = repo.commit_result_publication(tx, **args, content=content)
    SessionRepository().delete(
        tx,
        owner_id="owner",
        session_id="session-reference",
        expected_incarnation_id=args["authority"].credential.incarnation_id,
    )
    pubs = PublicationRepository()
    next_id = uid()
    now = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
    pubs.stage(
        tx,
        owner_id="owner",
        conversation_id=proposal.conversation_id,
        publication_id=next_id,
        request_generation=uid(),
        base_render_revision=1,
        started_at=now,
    )
    pubs.commit_at_head(
        tx,
        owner_id="owner",
        conversation_id=proposal.conversation_id,
        publication_id=next_id,
        expected_staged_base_render_revision=1,
        expected_head_render_revision=1,
        expected_head_publication_id=proposal.publication_id,
        committed_at=now,
        updated_at=int(now.timestamp()),
    )
    before = state(tx, record, proposal)
    replay = repo.prepare_result_publication(
        tx, **dict(args, authority=None, caller_valid_until=None)
    )
    assert replay.replayed and replay.receipt == receipt
    assert state(tx, record, proposal) == before


@pytest.mark.parametrize("expiry", ["authority", "deadline", "retired", "revoked"])
def test_new_save_cannot_adopt_lost_original_authority(tx, repo, expiry):
    record, proposal, content, args, _ = proposed(repo, tx)
    expire_authority(tx, record, expiry)
    before = state(tx, record, proposal)
    with pytest.raises(RepositoryConflictError):
        repo.commit_result_publication(tx, **args, content=content)
    assert state(tx, record, proposal) == before


@pytest.mark.parametrize("change", ["payload", "extra", "layout", "row", "decision", "state"])
def test_changed_content_destination_or_decision_never_publishes(tx, repo, change):
    record, proposal, content, args, _ = proposed(repo, tx)
    if change == "payload":
        content = replace(
            content, components=(replace(content.components[0], payload={"text": "other"}),)
        )
    if change == "extra":
        content = replace(
            content,
            components=(
                *content.components,
                PublicationRebaseComponent(
                    uid(), "hidden", {"text": "unreviewed"}, "text", None, 1
                ),
            ),
        )
    if change == "layout":
        content = replace(content, layouts=())
    if change == "row":
        content = replace(content, components=(replace(content.components[0], row_id=uid()),))
    if change == "decision":
        args = dict(args, decision=replace(args["decision"], proposal_digest=digest("other")))
    if change == "state":
        args = dict(args, expected_state_version=args["expected_state_version"] - 1)
    before = state(tx, record, proposal)
    with pytest.raises(RepositoryConflictError):
        repo.commit_result_publication(tx, **args, content=content)
    assert state(tx, record, proposal) == before


def test_preexisting_stage_is_never_adopted(tx, repo):
    record, proposal, content, args, _ = proposed(repo, tx)
    PublicationRepository().stage(
        tx,
        owner_id="owner",
        conversation_id=proposal.conversation_id,
        publication_id=proposal.publication_id,
        request_generation=uid(),
        base_render_revision=0,
        started_at=tx.fetch_one("SELECT clock_timestamp() AS now")["now"],
    )
    before = state(tx, record, proposal)
    with pytest.raises(RepositoryConflictError):
        repo.commit_result_publication(tx, **args, content=content)
    assert state(tx, record, proposal) == before


def test_post_commit_component_tamper_is_not_a_valid_receipt_read(tx, repo):
    _record, proposal, content, args, _ = proposed(repo, tx)
    repo.commit_result_publication(tx, **args, content=content)
    row = CanvasRepository().list_current(
        tx, owner_id="owner", conversation_id=proposal.conversation_id
    )[0]
    CanvasRepository().replace(
        tx,
        owner_id="owner",
        conversation_id=proposal.conversation_id,
        component_id="result",
        payload={"text": "changed"},
        component_type="text",
        title=None,
        expected_updated_at=row.updated_at,
        updated_at=row.updated_at + 1,
        publication_id=proposal.publication_id,
        committed_render_revision=1,
    )
    with pytest.raises(RepositoryConflictError):
        repo.prepare_result_publication(tx, **dict(args, authority=None, caller_valid_until=None))


def test_worker_cannot_create_reserved_owner_publication_subtype(tx, repo):
    create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    with pytest.raises(RepositoryValidationError):
        action(repo, tx, claim.fence, request={"kind": "result_publication"})


def test_new_save_final_failure_rolls_back_pointer_receipt_and_outer_audit(tx, repo, monkeypatch):
    record, proposal, content, args, _ = proposed(repo, tx)
    tx.execute("CREATE TEMP TABLE save_audit(event TEXT)")
    before = state(tx, record, proposal)
    original = repo.assert_selected_input_current
    calls = 0

    def refuse_after_pointer(*values, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RepositoryConflictError("synthetic final guard refusal")
        return original(*values, **kwargs)

    monkeypatch.setattr(repo, "assert_selected_input_current", refuse_after_pointer)
    with pytest.raises(RepositoryConflictError), tx.savepoint("host_audit_and_save"):
        tx.execute("INSERT INTO save_audit VALUES('owner approved exact result')")
        repo.commit_result_publication(tx, **args, content=content)
    assert state(tx, record, proposal) == before
    assert tx.fetch_all("SELECT * FROM save_audit") == ()
    assert (
        tx.fetch_one(
            "SELECT commit_id FROM conversation_commit WHERE commit_id=%s",
            (proposal.publication_id,),
        )
        is None
    )
    assert "Exact public result" not in canonical(before)
