"""Append-only enforcement tests (FR-014 / AU-9).

Direct UPDATE/DELETE against ``audit_events`` MUST raise unless the
session has set ``audit.allow_purge = 'true'`` (held only by the
retention CLI). The application repository never sets that GUC.
"""
from __future__ import annotations

import pytest


def test_direct_update_is_blocked_by_trigger(repo, make_event, unique_user, database):
    e = repo.insert(make_event(actor_user_id=unique_user, auth_principal=unique_user))
    with pytest.raises(Exception), database.transaction() as transaction:
        transaction.execute(
            "UPDATE audit_events SET description = %s WHERE event_id = %s",
            ("tamper", e.event_id),
        )


def test_direct_delete_is_blocked_by_trigger(repo, make_event, unique_user, database):
    e = repo.insert(make_event(actor_user_id=unique_user, auth_principal=unique_user))
    with pytest.raises(Exception), database.transaction() as transaction:
        transaction.execute(
            "DELETE FROM audit_events WHERE event_id = %s",
            (e.event_id,),
        )


def test_purge_path_holds_guc_and_succeeds(repo, make_event, unique_user, database):
    e = repo.insert(make_event(actor_user_id=unique_user, auth_principal=unique_user))
    with database.transaction() as transaction:
        transaction.execute("SET LOCAL audit.allow_purge = 'true'")
        transaction.execute(
            "DELETE FROM audit_events WHERE event_id = %s",
            (e.event_id,),
        )
    assert repo.get_for_user(unique_user, e.event_id) is None
