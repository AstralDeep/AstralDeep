"""Filter & cursor pagination tests for ``AuditRepository.list_for_user``.

Backs the contract obligations in
``specs/003-agent-audit-log/contracts/rest-audit-api.md``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _seed(repo, make_event, user, count, **overrides):
    out = []
    for i in range(count):
        out.append(repo.insert(make_event(
            actor_user_id=user, auth_principal=user,
            action_type=f"auth.event_{i}", **overrides,
        )))
    return out


def test_list_returns_most_recent_first(repo, make_event, unique_user):
    events = _seed(repo, make_event, unique_user, 4)
    items, _ = repo.list_for_user(unique_user, limit=10)
    # The first item should be the most recently inserted
    assert items[0].action_type == events[-1].action_type


def test_filter_by_event_class(repo, make_event, unique_user):
    repo.insert(make_event(
        actor_user_id=unique_user, auth_principal=unique_user,
        event_class="auth", action_type="auth.x",
    ))
    repo.insert(make_event(
        actor_user_id=unique_user, auth_principal=unique_user,
        event_class="conversation", action_type="ws.chat_message",
    ))
    items, _ = repo.list_for_user(unique_user, limit=10, event_classes=["conversation"])
    assert all(i.event_class == "conversation" for i in items)
    assert any(i.action_type == "ws.chat_message" for i in items)


def test_filter_by_outcome(repo, make_event, unique_user):
    repo.insert(make_event(actor_user_id=unique_user, auth_principal=unique_user, outcome="success", action_type="auth.ok"))
    repo.insert(make_event(actor_user_id=unique_user, auth_principal=unique_user, outcome="failure", action_type="auth.bad", outcome_detail="bad"))
    items, _ = repo.list_for_user(unique_user, limit=10, outcomes=["failure"])
    assert all(i.outcome == "failure" for i in items)


def test_filter_by_keyword(repo, make_event, unique_user):
    repo.insert(make_event(actor_user_id=unique_user, auth_principal=unique_user, description="alpha keyword phrase"))
    repo.insert(make_event(actor_user_id=unique_user, auth_principal=unique_user, description="beta unrelated"))
    items, _ = repo.list_for_user(unique_user, limit=10, keyword="keyword")
    assert any("keyword" in i.description.lower() for i in items)
    assert all("keyword" in i.description.lower() or "keyword" in i.action_type.lower() for i in items)


def test_cursor_pagination_yields_disjoint_pages(repo, make_event, unique_user):
    _seed(repo, make_event, unique_user, 7)
    page1, cursor = repo.list_for_user(unique_user, limit=3)
    assert len(page1) == 3
    assert cursor is not None
    page2, cursor2 = repo.list_for_user(unique_user, limit=3, cursor=cursor)
    assert len(page2) == 3
    seen = {e.event_id for e in page1} | {e.event_id for e in page2}
    assert len(seen) == 6  # disjoint
    page3, cursor3 = repo.list_for_user(unique_user, limit=3, cursor=cursor2)
    assert len(page3) >= 1
    # last page: cursor3 may or may not be set depending on exact count


def test_invalid_cursor_raises(repo, unique_user):
    import pytest
    with pytest.raises(ValueError):
        repo.list_for_user(unique_user, limit=10, cursor="not-a-cursor")


def test_purge_older_than_only_drops_old_rows(repo, make_event, unique_user, database):
    # Retention keeps one authenticated boundary row and prunes only the named
    # owner's expired prefix.
    expired = repo.insert(
        make_event(actor_user_id=unique_user, auth_principal=unique_user)
    )
    retained = repo.insert(
        make_event(actor_user_id=unique_user, auth_principal=unique_user)
    )
    other_user = f"{unique_user}-other"
    other = repo.insert(
        make_event(actor_user_id=other_user, auth_principal=other_user)
    )

    with database.transaction() as transaction:
        transaction.execute("SET LOCAL audit.allow_purge = 'true'")
        transaction.execute(
            "UPDATE audit_events "
            "SET recorded_at = recorded_at - INTERVAL '7 years' "
            "WHERE event_id = ANY(%s::uuid[])",
            ([expired.event_id, other.event_id],),
        )

    cutoff = datetime.now(timezone.utc) - timedelta(days=365 * 6)
    deleted = repo.purge_older_than(unique_user, cutoff)
    assert deleted == 1
    assert repo.get_for_user(unique_user, expired.event_id) is None
    assert repo.get_for_user(unique_user, retained.event_id) is not None
    assert repo.get_for_user(other_user, other.event_id) is not None
