"""Tests for astralplane.repositories.assignments, guidance, and history: publication
requires exact receipt/result provenance, and a retired selection or mismatched
approval never earns or reconsumes a save.
"""

from dataclasses import replace

import pytest
from test_assignments_postgres import database as database
from test_assignments_postgres import repo as repo
from test_assignments_postgres import session_observation, uid
from test_assignments_postgres import tx as tx
from test_guidance_storage_postgres import note
from test_operation_terminal_postgres import change_action
from test_result_publication_postgres import proposed, state
from test_selected_input_postgres import bind_selected, envelope, selected_agent

from astralplane.repositories import RepositoryConflictError, RepositoryNotFoundError
from astralplane.repositories.assignments import canonical, digest, plain
from astralplane.repositories.guidance import ExplicitNotesRepository
from astralplane.repositories.guidance_models import GuidanceReference
from astralplane.repositories.history import SessionRepository

# Must stay a tests.* import: bare name is sys.path-order dependent
from tests.repositories.test_declarative_agents_postgres import apply
from tests.repositories.test_declarative_agents_postgres import command as declaration


@pytest.mark.parametrize(
    "change",
    [
        "future",
        "request",
        "result",
        "state",
        "epoch",
        "opaque_liability",
        "issued_liability",
        "outstanding",
    ],
)
def test_result_mismatch_or_unknown_liability_never_earns_publication(tx, repo, change):
    record, proposal, content, args, _ = proposed(repo, tx)
    model = plain(
        tx.fetch_one(
            "SELECT data FROM persistent_assignment_action WHERE id=%s",
            (proposal.result_action_id,),
        )["data"]
    )
    if change == "future":
        model["future_extension"] = True
    if change == "request":
        model["intent"]["request"]["kind"] = "tool"
    if change == "result":
        model["result"]["result_digest"] = digest("other")
    if change == "state":
        model["state"] = "uncertain"
    if change == "epoch":
        model["control_epoch"] += 1
    if change in {"future", "request", "result", "state", "epoch"}:
        tx.execute(
            "UPDATE persistent_assignment_action SET data=%s::jsonb,state=%s WHERE id=%s",
            (canonical(model), model["state"], proposal.result_action_id),
        )
    elif change in {"opaque_liability", "issued_liability"}:
        inherited = plain(model)
        inherited["action_id"] = uid()
        inherited["intent"]["action_key"] = uid()
        inherited["intent_digest"] = digest(inherited["intent"])
        if change == "opaque_liability":
            inherited["future_extension"] = {"effect": "unknown"}
        else:
            inherited["state"] = "started"
            inherited["attempts"][-1]["state"] = "started"
            inherited["attempts"][-1]["outcome"] = None
            inherited["result"] = None
        tx.execute(
            "INSERT INTO persistent_assignment_action "
            "(id,assignment_id,owner_user_id,action_key,state,data) "
            "VALUES(%s,%s,'owner',%s,%s,%s::jsonb)",
            (
                inherited["action_id"],
                record.assignment_id,
                inherited["intent"]["action_key"],
                inherited["state"],
                canonical(inherited),
            ),
        )
    else:
        data = plain(state(tx, record, proposal)[0]["data"])
        data["usage"]["outstanding"]["tokens"] = 1
        tx.execute(
            "UPDATE persistent_assignment SET data=%s::jsonb WHERE id=%s",
            (canonical(data), record.assignment_id),
        )
    before = state(tx, record, proposal)
    ledger = tx.fetch_all("SELECT id,data,state FROM persistent_assignment_action ORDER BY id")
    with pytest.raises(RepositoryConflictError):
        repo.commit_result_publication(tx, **args, content=content)
    assert state(tx, record, proposal) == before
    assert (
        tx.fetch_all("SELECT id,data,state FROM persistent_assignment_action ORDER BY id") == ledger
    )


@pytest.mark.parametrize(
    "change", ["extra", "version", "decision", "timestamp", "counter", "result"]
)
def test_corrupt_owner_receipt_remains_an_unknown_liability(tx, repo, change):
    record, proposal, content, args, _ = proposed(repo, tx)
    repo.commit_result_publication(tx, **args, content=content)

    def mutate(action):
        if change == "extra":
            action["publication_receipt"]["private_extra"] = "refuse"
        elif change == "version":
            action["publication_receipt"]["version"] = True
        elif change == "decision":
            action["decision"]["submission_id"] = uid()
        elif change == "timestamp":
            action["approval_consumed_at"] = None
        elif change == "counter":
            action["instruction_revision"] = True
        else:
            action["result"] = {"fake_worker_success": True}

    change_action(tx, proposal.action_id, mutate)
    assert repo._known_action(tx, "owner", record.assignment_id, proposal.action_id) is None
    data = repo._load(tx, "owner", record.assignment_id)
    _, unknown, held = repo._purge_blockers(tx, data)
    assert proposal.action_id in unknown and held
    with pytest.raises(RepositoryConflictError):
        repo.prepare_result_publication(tx, **dict(args, authority=None, caller_valid_until=None))


@pytest.mark.parametrize("kind", ["agent", "note"])
@pytest.mark.parametrize("accepted", [False, True])
def test_exact_selection_retirement_refuses_new_save_but_not_accepted_receipt(
    tx, repo, kind, accepted
):
    chosen = {}

    def select(tx, record):
        if kind == "agent":
            head, ref = selected_agent(tx)
            chosen.update(head=head, ref=ref)
            value = envelope(ref)
        else:
            head = note(tx)
            chosen["note"] = head
            value = envelope(refs=(GuidanceReference("note", head.note_id, 1),))
        return bind_selected(repo, tx, record, value)

    record, proposal, content, args, _ = proposed(repo, tx, select=select)
    receipt = repo.commit_result_publication(tx, **args, content=content) if accepted else None
    if kind == "agent":
        apply(
            tx,
            declaration(
                "archive",
                owner_id="owner",
                agent_id=chosen["ref"].agent_id,
                expected_revision=chosen["head"].agent.state_revision,
            ),
        )
    else:
        ExplicitNotesRepository().forget_explicit_note(
            tx, owner_id="owner", note_id=chosen["note"].note_id, expected_revision=1
        )
    before = state(tx, record, proposal)
    if accepted:
        replay = repo.prepare_result_publication(
            tx, **dict(args, authority=None, caller_valid_until=None)
        )
        assert replay.replayed and replay.receipt == receipt
    else:
        with pytest.raises(RepositoryConflictError):
            repo.commit_result_publication(tx, **args, content=content)
    assert state(tx, record, proposal) == before


def test_same_sid_replacement_and_other_owner_cannot_adopt_save(tx, repo):
    record, proposal, content, args, _ = proposed(repo, tx)
    SessionRepository().delete(
        tx,
        owner_id="owner",
        session_id="session-reference",
        expected_incarnation_id=args["authority"].credential.incarnation_id,
    )
    new = session_observation(tx)
    assert new.credential.incarnation_id != args["authority"].credential.incarnation_id
    before = state(tx, record, proposal)
    with pytest.raises(RepositoryConflictError):
        repo.commit_result_publication(tx, **dict(args, authority=new), content=content)
    with pytest.raises(RepositoryNotFoundError):
        repo.prepare_result_publication(tx, **dict(args, owner_id="other"))
    assert state(tx, record, proposal) == before


def test_different_approval_cannot_reconsume_committed_action(tx, repo):
    record, proposal, content, args, _ = proposed(repo, tx)
    repo.commit_result_publication(tx, **args, content=content)
    before = state(tx, record, proposal)
    with pytest.raises(RepositoryConflictError):
        repo.prepare_result_publication(
            tx, **dict(args, decision=replace(args["decision"], submission_id=uid()))
        )
    assert state(tx, record, proposal) == before


def test_unknown_proposal_is_not_an_owner_read_success(tx, repo):
    _record, _proposal, _content, args, _ = proposed(repo, tx)
    with pytest.raises(RepositoryNotFoundError):
        repo.prepare_result_publication(tx, **dict(args, action_id=uid()))


def test_original_authority_required_on_every_receipt_miss(tx, repo):
    record, proposal, content, args, _ = proposed(repo, tx)
    before = state(tx, record, proposal)
    with pytest.raises(RepositoryConflictError):
        repo.commit_result_publication(tx, **dict(args, authority=None), content=content)
    with pytest.raises(RepositoryConflictError):
        repo.commit_result_publication(tx, **dict(args, caller_valid_until=None), content=content)
    assert state(tx, record, proposal) == before
