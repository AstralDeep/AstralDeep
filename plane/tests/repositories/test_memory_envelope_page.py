"""Checks bounded structured-memory pagination through PersonalizationRepository.
Real PostgreSQL covers filtering, ownership and keysets; observed queries cover preflight refusals.
"""

import json
from dataclasses import replace
from uuid import uuid4

import pytest
from test_preferences import FakeTransaction, memory_record

from astralplane.repositories import RepositoryValidationError, preferences


def envelope(kind="head", format_id="test.document/v1", owner="owner", project="project"):
    return json.dumps({"format": format_id, "kind": kind, "owner_id": owner,
                       "project_id": project, "opaque": "synthetic-ciphertext"})


def add(repo, tx, *, owner="owner", project="project", moment=10, value=None):
    return repo.create_memory(tx, memory_record(
        memory_id=str(uuid4()), owner_id=owner, project_id=project,
        created_at=moment, updated_at=moment,
        value=envelope(owner=owner, project=project) if value is None else value,
    ))


def listing(repo, tx, **changes):
    args = dict(owner_id="owner", project_id="project", envelope_format="test.document/v1",
                envelope_kind="head", limit=2)
    args.update(changes)
    return repo.list_memory_envelope_page(tx, **args)


def test_envelope_page_filters_before_limit_and_continues_tied_keys(migrated_clone):
    repo = preferences.PersonalizationRepository()
    with migrated_clone.database.transaction() as tx:
        heads = [add(repo, tx, moment=10) for _ in range(3)]
        for index in range(101):
            add(repo, tx, moment=20 + index, value="Ordinary memory")
        for value in ["{broken", "null", "[]", envelope(kind="revision"),
                      envelope(format_id="different"), '{"format":1,"kind":"head"}']:
            add(repo, tx, moment=200, value=value)
        expected = sorted((h.memory_id for h in heads), reverse=True)
        first = listing(repo, tx)
        assert [r.memory_id for r in first.records] == expected[:2]
        assert first.next_cursor is not None
        second = listing(repo, tx, cursor=first.next_cursor)
        assert [r.memory_id for r in second.records] == expected[2:]
        assert second.next_cursor is None
        assert listing(repo, tx, cursor=replace(first.next_cursor, created_at=0)).records == ()


def test_forged_or_missing_binding_headers_cannot_consume_the_page_limit(migrated_clone):
    repo = preferences.PersonalizationRepository()
    with migrated_clone.database.transaction() as tx:
        valid = add(repo, tx)
        for index in range(101):
            add(repo, tx, moment=20 + index, value=envelope(owner="foreign"))
        for field in ["owner_id", "project_id"]:
            for wrong in [None, 1, True, [], {}, "other"]:
                data = json.loads(envelope())
                data[field] = wrong
                add(repo, tx, moment=200, value=json.dumps(data))
            data.pop(field)
            add(repo, tx, moment=200, value=json.dumps(data))
        assert listing(repo, tx, limit=1).records == (valid,)
        global_record = add(repo, tx, project=None)
        missing = json.loads(envelope(project=None))
        del missing["project_id"]
        add(repo, tx, project=None, moment=300, value=json.dumps(missing))
        add(repo, tx, project=None, moment=300, value=envelope(project="project"))
        assert listing(repo, tx, project_id=None).records == (global_record,)


def test_envelope_page_scopes_owner_project_and_ignores_superseded(migrated_clone):
    repo = preferences.PersonalizationRepository()
    with migrated_clone.database.transaction() as tx:
        mine = add(repo, tx)
        add(repo, tx, owner="foreign")
        add(repo, tx, project="other")
        global_head = add(repo, tx, project=None)
        obsolete = add(repo, tx)
        repo.supersede_memory(tx, owner_id="owner", memory_id=obsolete.memory_id,
                              superseded_at=100)
        assert listing(repo, tx).records == (mine,)
        assert listing(repo, tx).next_cursor is None
        assert listing(repo, tx, project_id=None).records == (global_head,)
        assert listing(repo, tx, owner_id="absent").records == ()


def test_exact_capacity_has_no_false_continuation_and_newer_insert_is_not_repeated(migrated_clone):
    repo = preferences.PersonalizationRepository()
    with migrated_clone.database.transaction() as tx:
        original = add(repo, tx)
        assert listing(repo, tx, limit=1).next_cursor is None
        newer = add(repo, tx, moment=11)
        first = listing(repo, tx, limit=1)
        assert first.records == (newer,)
        add(repo, tx, moment=12)
        assert listing(repo, tx, cursor=first.next_cursor).records == (original,)


@pytest.mark.parametrize("changes", [
    {"owner_id": ""}, {"project_id": ""}, {"envelope_format": ""},
    {"envelope_kind": ""}, {"envelope_format": "x" * 129},
    {"envelope_kind": "x" * 129}, {"limit": True}, {"limit": 0},
    {"limit": 101}, {"limit": 1.5}, {"limit": "2"}, {"cursor": object()},
])
def test_envelope_page_refuses_invalid_inputs_before_query(changes):
    query = FakeTransaction()
    with pytest.raises(RepositoryValidationError):
        listing(preferences.PersonalizationRepository(), query, **changes)
    assert query.calls == []


@pytest.mark.parametrize("changes", [
    {"owner_id": "foreign"}, {"project_id": "other"},
    {"envelope_format": "different"}, {"envelope_kind": "revision"},
    {"created_at": True}, {"created_at": -1}, {"created_at": 2 ** 63},
    {"memory_id": "invalid"}, {"memory_id": True},
])
def test_envelope_page_refuses_invalid_or_foreign_cursor_before_query(changes):
    cursor = preferences.MemoryEnvelopeCursor(
        owner_id="owner", project_id="project", envelope_format="test.document/v1",
        envelope_kind="head", created_at=10, memory_id=str(uuid4()),
    )
    query = FakeTransaction()
    with pytest.raises(RepositoryValidationError):
        listing(preferences.PersonalizationRepository(), query, cursor=replace(cursor, **changes))
    assert query.calls == []
