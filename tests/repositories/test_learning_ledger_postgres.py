"""Exercises the learning ledger against real PostgreSQL: the proposal, adoption and
reversal order, idempotent replay by event id, the evidence and review requirements, the
reads for adopted items and proposals awaiting a decision, and owner isolation.
"""

import pytest

from astralplane import create_learning_ledger_repository
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from tests.fixtures.database_errors import INTEGRITY_ERRORS
from tests.repositories._mesh_support import OTHER, OWNER, genesis, ref

repo = create_learning_ledger_repository()
PROPOSAL = ref(b"proposal-1")
ADOPTION = ref(b"adoption-1")
REVERSAL = ref(b"reversal-1")
EVIDENCE = [ref(b"turn-1"), ref(b"turn-2")]
CONTENT = ref(b"skill text")


def propose(tx, mesh, event=PROPOSAL, owner=OWNER, **changes):
    values = {
        "item_kind": "skill",
        "content_hash": CONTENT,
        "scope": "project-a",
        "evidence": EVIDENCE,
        "recorded_at": 10,
    }
    values.update(changes)
    return repo.record_proposal(tx, owner_id=owner, mesh_id=mesh, event_id=event, **values)


def adopt(tx, mesh, event=ADOPTION, proposal=PROPOSAL, owner=OWNER, **changes):
    values = {
        "scope": "project-a",
        "review": "owner",
        "evidence": [ref(b"replay")],
        "recorded_at": 11,
    }
    values.update(changes)
    return repo.record_adoption(
        tx, owner_id=owner, mesh_id=mesh, event_id=event, proposal_id=proposal, **values
    )


def reverse(tx, mesh, event=REVERSAL, proposal=PROPOSAL, owner=OWNER, **changes):
    values = {"evidence": [ref(b"regression")], "recorded_at": 12}
    values.update(changes)
    return repo.record_reversal(
        tx, owner_id=owner, mesh_id=mesh, event_id=event, proposal_id=proposal, **values
    )


def reason(raised):
    return dict(raised.value.metadata)["reason"]


def test_a_learned_item_moves_from_proposal_to_adoption_to_reversal(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
        proposal = propose(tx, mesh)
        assert proposal.proposal_id == PROPOSAL and proposal.review is None
        assert proposal.evidence == tuple(sorted(EVIDENCE))
        item = repo.get_item(tx, owner_id=OWNER, mesh_id=mesh, proposal_id=PROPOSAL)
        assert item.status == "proposed"
        assert [
            e.event_id for e in repo.list_awaiting_decision(tx, owner_id=OWNER, mesh_id=mesh)
        ] == [PROPOSAL]
        adoption = adopt(tx, mesh, scope="project-a/tool-x", review="automatic")
        assert (adoption.content_hash, adoption.item_kind, adoption.review) == (
            CONTENT,
            "skill",
            "automatic",
        )
        assert (
            repo.get_item(tx, owner_id=OWNER, mesh_id=mesh, proposal_id=PROPOSAL).status
            == "adopted"
        )
        assert repo.list_awaiting_decision(tx, owner_id=OWNER, mesh_id=mesh) == ()
        assert [e.event_id for e in repo.list_adopted(tx, owner_id=OWNER, mesh_id=mesh)] == [
            ADOPTION
        ]
        assert [
            e.event_id
            for e in repo.list_adopted(tx, owner_id=OWNER, mesh_id=mesh, scope="project-a/tool-x")
        ] == [ADOPTION]
        assert repo.list_adopted(tx, owner_id=OWNER, mesh_id=mesh, scope="elsewhere") == ()
        reversal = reverse(tx, mesh)
        assert (reversal.scope, reversal.content_hash) == ("project-a/tool-x", CONTENT)
        final = repo.get_item(tx, owner_id=OWNER, mesh_id=mesh, proposal_id=PROPOSAL)
        assert final.status == "reversed" and final.reversal == reversal
        assert repo.list_adopted(tx, owner_id=OWNER, mesh_id=mesh) == ()
        assert repo.get_entry(tx, owner_id=OWNER, mesh_id=mesh, event_id=ADOPTION) == adoption


def test_replays_are_idempotent_and_changed_replays_conflict(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
        proposal = propose(tx, mesh)
        assert propose(tx, mesh, recorded_at=99) == proposal
        with pytest.raises(RepositoryConflictError, match="different semantics"):
            propose(tx, mesh, scope="another")
        adoption = adopt(tx, mesh)
        assert adopt(tx, mesh) == adoption
        with pytest.raises(RepositoryConflictError, match="different semantics"):
            adopt(tx, mesh, review="automatic")
        reversal = reverse(tx, mesh)
        assert reverse(tx, mesh) == reversal


def test_decisions_follow_the_order_and_happen_once(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
        with pytest.raises(RepositoryNotFoundError, match="proposal"):
            adopt(tx, mesh)
        propose(tx, mesh)
        with pytest.raises(RepositoryConflictError) as early:
            reverse(tx, mesh)
        assert reason(early) == "not_adopted"
        adopt(tx, mesh)
        with pytest.raises(RepositoryConflictError) as twice:
            adopt(tx, mesh, event=ref(b"adoption-2"))
        assert reason(twice) == "already_decided"
        reverse(tx, mesh)
        with pytest.raises(RepositoryConflictError) as again:
            reverse(tx, mesh, event=ref(b"reversal-2"))
        assert reason(again) == "already_reversed"
        with pytest.raises(RepositoryConflictError) as readopt:
            adopt(tx, mesh, event=ref(b"adoption-3"))
        assert reason(readopt) == "already_decided"


def test_inputs_are_validated(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
        for changes in (
            {"item_kind": "plugin"},
            {"content_hash": "sha256:short"},
            {"scope": ""},
            {"evidence": []},
            {"evidence": [EVIDENCE[0], EVIDENCE[0]]},
            {"evidence": "text"},
            {"evidence": ["nope"]},
            {"evidence": [ref(str(n).encode()) for n in range(65)]},
            {"recorded_at": -1},
        ):
            with pytest.raises(RepositoryValidationError):
                propose(tx, mesh, **changes)
        propose(tx, mesh)
        with pytest.raises(RepositoryValidationError):
            adopt(tx, mesh, review="auto")
        with pytest.raises(RepositoryValidationError):
            adopt(tx, mesh, proposal="proposal-1")


def test_owners_are_isolated(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
        genesis(tx, owner=OTHER)
        propose(tx, mesh)
        adoption = adopt(tx, mesh)
        assert repo.get_entry(tx, owner_id=OTHER, mesh_id=mesh, event_id=ADOPTION) is None
        assert repo.get_item(tx, owner_id=OTHER, mesh_id=mesh, proposal_id=PROPOSAL) is None
        assert repo.list_adopted(tx, owner_id=OTHER, mesh_id=mesh) == ()
        assert repo.list_awaiting_decision(tx, owner_id=OTHER, mesh_id=mesh) == ()
        with pytest.raises(RepositoryNotFoundError):
            adopt(tx, mesh, event=ref(b"foreign"), owner=OTHER)
        with pytest.raises(RepositoryNotFoundError):
            propose(tx, mesh, event=ref(b"foreign-proposal"), owner=OTHER)
        assert repo.get_entry(tx, owner_id=OWNER, mesh_id=mesh, event_id=ADOPTION) == adoption


def test_database_enforces_the_ledger_shapes(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
        propose(tx, mesh)
    insert = (
        "INSERT INTO learning_ledger_entry (owner_id, mesh_id, event_id, kind, proposal_id, "
        "item_kind, content_hash, scope, review, evidence, recorded_at) "
        "VALUES (%s, %s, %s, %s, %s, 'skill', %s, 's', %s, %s::jsonb, 1)"
    )
    cases = (
        (ref(b"p2"), "proposal", PROPOSAL, None, '["sha256:' + "a" * 64 + '"]'),
        (ref(b"a2"), "adoption", PROPOSAL, None, '["sha256:' + "a" * 64 + '"]'),
        (ref(b"a3"), "adoption", PROPOSAL, "owner", "[]"),
    )
    for event, kind, proposal, review, evidence in cases:
        with pytest.raises(INTEGRITY_ERRORS), migrated_clone.database.transaction() as tx:
            tx.execute(insert, (OWNER, mesh, event, kind, proposal, CONTENT, review, evidence))


def test_public_learning_ledger_contract_behaviors():
    from tests.fixtures.migrated_template import bound_clone, current_lineages

    for check in (
        test_a_learned_item_moves_from_proposal_to_adoption_to_reversal,
        test_replays_are_idempotent_and_changed_replays_conflict,
        test_decisions_follow_the_order_and_happen_once,
        test_owners_are_isolated,
    ):
        with bound_clone(current_lineages().current()) as clone:
            check(clone)
