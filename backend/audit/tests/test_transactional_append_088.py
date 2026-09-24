"""Tests for audit/repository.py's insert_in_transaction: audit rows join the caller's
own transaction, roll back with it on abort or authenticator failure, and never
commit on a DTO validation failure.
"""

from __future__ import annotations

import pytest

from audit import repository as audit_repository


def event(make_event, owner, action):
    return make_event(actor_user_id=owner, auth_principal=owner, action_type=action)


def test_caller_transaction_appends_to_normal_chain_without_nested_checkout(
    repo, database, make_event, unique_user, monkeypatch,
):
    first = repo.insert(event(make_event, unique_user, "auth.before"))

    def no_nested_checkout():
        raise AssertionError("audit opened a second transaction")

    with database.transaction() as transaction:
        with monkeypatch.context() as patch:
            patch.setattr(repo._audit, "transaction", no_nested_checkout)
            second = repo.insert_in_transaction(event(make_event, unique_user, "auth.atomic"),
                transaction=transaction, plane_runtime=database)
            third = repo.insert_in_transaction(event(make_event, unique_user, "auth.following"),
                transaction=transaction, plane_runtime=database)
    assert len({first.event_id, second.event_id, third.event_id}) == 3
    assert second.action_type == "auth.atomic"
    assert repo.get_for_user(unique_user, second.event_id) == second
    assert repo.verify_chain(unique_user) is None
    rows, _ = repo.list_for_user(unique_user)
    assert {row.event_id for row in rows} == {first.event_id, second.event_id, third.event_id}


def test_provisional_results_disappear_when_caller_aborts(
    repo, database, make_event, unique_user,
):
    with pytest.raises(RuntimeError, match="caller refused commit"):
        with database.transaction() as transaction:
            first = repo.insert_in_transaction(event(make_event, unique_user, "auth.first"),
                transaction=transaction, plane_runtime=database)
            second = repo.insert_in_transaction(event(make_event, unique_user, "auth.second"),
                transaction=transaction, plane_runtime=database)
            raise RuntimeError("caller refused commit")
    assert repo.get_for_user(unique_user, first.event_id) is None
    assert repo.get_for_user(unique_user, second.event_id) is None
    assert repo.list_for_user(unique_user) == ([], None)
    assert repo.verify_chain(unique_user) is None


def test_authenticator_failure_rolls_back_prior_append_without_retry(
    repo, database, make_event, unique_user, monkeypatch,
):
    calls = 0

    def unavailable(*args):
        nonlocal calls
        calls += 1
        raise RuntimeError("test authenticator unavailable")

    with pytest.raises(RuntimeError, match="test authenticator unavailable"):
        with database.transaction() as transaction:
            prior = repo.insert_in_transaction(event(make_event, unique_user, "auth.prior"),
                transaction=transaction, plane_runtime=database)
            with monkeypatch.context() as patch:
                patch.setattr(audit_repository, "_authenticate", unavailable)
                repo.insert_in_transaction(event(make_event, unique_user, "auth.failed"),
                    transaction=transaction, plane_runtime=database)
    assert calls == 1
    assert repo.get_for_user(unique_user, prior.event_id) is None
    assert repo.list_for_user(unique_user) == ([], None)


@pytest.mark.parametrize("invalid", ["foreign_runtime", "missing_runtime", "missing_transaction", "untyped_event"])
def test_invalid_context_refuses_before_append(
    repo, database, make_event, unique_user, monkeypatch, invalid,
):
    def forbidden(*args):
        raise AssertionError("invalid context reached append")

    monkeypatch.setattr(repo._audit.repository, "append", forbidden)
    with database.transaction() as transaction:
        arguments = dict(event=event(make_event, unique_user, "auth.invalid"),
                         transaction=transaction, plane_runtime=database)
        if invalid == "foreign_runtime":
            arguments["plane_runtime"] = object()
        elif invalid == "missing_runtime":
            arguments["plane_runtime"] = None
        elif invalid == "missing_transaction":
            arguments["transaction"] = None
        else:
            arguments["event"] = arguments["event"].model_dump()
        with pytest.raises(ValueError, match="audit transaction context unavailable"):
            repo.insert_in_transaction(**arguments)


def test_dto_failure_cannot_commit_the_required_audit(
    repo, database, make_event, unique_user, monkeypatch,
):
    def refuse(*args):
        raise RuntimeError("test projection failure")

    with monkeypatch.context() as patch:
        patch.setattr(audit_repository, "_record_to_dto", refuse)
        with pytest.raises(RuntimeError, match="test projection failure"):
            with database.transaction() as transaction:
                repo.insert_in_transaction(event(make_event, unique_user, "auth.projection"),
                    transaction=transaction, plane_runtime=database)
    assert repo.list_for_user(unique_user) == ([], None)
