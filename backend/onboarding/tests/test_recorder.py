"""Recorder unit tests — every onboarding wrapper writes one audit row."""
from __future__ import annotations

import asyncio

import pytest

from audit.recorder import Recorder, set_recorder
from audit.repository import AuditRepository
from onboarding.recorder import (
    record_onboarding_completed,
    record_onboarding_replayed,
    record_onboarding_skipped,
    record_onboarding_started,
    record_tutorial_step_edited,
)


@pytest.fixture
def wired_recorder(database):
    repo = AuditRepository(
        plane_runtime=database,
        plane_repositories=database.repositories,
    )
    rec = Recorder(repo)
    set_recorder(rec)
    yield rec
    set_recorder(None)


def _events_for_user(database, user_id, event_class):
    with database.transaction() as transaction:
        return list(
            transaction.fetch_all(
            """
            SELECT event_class, action_type, inputs_meta
            FROM audit_events
            WHERE actor_user_id = %s AND event_class = %s
            ORDER BY recorded_at DESC
            """,
            (user_id, event_class),
        )
        )


def test_record_onboarding_started(wired_recorder, database, unique_user):
    asyncio.run(record_onboarding_started(
        actor_user_id=unique_user, auth_principal=unique_user, step_slug="welcome",
    ))
    rows = _events_for_user(database, unique_user, "onboarding_started")
    assert len(rows) == 1
    assert rows[0]["action_type"] == "onboarding.start"
    assert rows[0]["inputs_meta"]["step_slug"] == "welcome"


def test_record_onboarding_completed(wired_recorder, database, unique_user):
    asyncio.run(record_onboarding_completed(
        actor_user_id=unique_user, auth_principal=unique_user, last_step_slug="finish",
    ))
    rows = _events_for_user(database, unique_user, "onboarding_completed")
    assert len(rows) == 1
    assert rows[0]["inputs_meta"]["last_step_slug"] == "finish"


def test_record_onboarding_skipped(wired_recorder, database, unique_user):
    asyncio.run(record_onboarding_skipped(
        actor_user_id=unique_user, auth_principal=unique_user, last_step_slug="open-audit-log",
    ))
    rows = _events_for_user(database, unique_user, "onboarding_skipped")
    assert len(rows) == 1
    assert rows[0]["inputs_meta"]["last_step_slug"] == "open-audit-log"


def test_record_onboarding_replayed(wired_recorder, database, unique_user):
    asyncio.run(record_onboarding_replayed(
        actor_user_id=unique_user, auth_principal=unique_user, prior_status="completed",
    ))
    rows = _events_for_user(database, unique_user, "onboarding_replayed")
    assert len(rows) == 1
    assert rows[0]["inputs_meta"]["prior_status"] == "completed"


def test_record_tutorial_step_edited(wired_recorder, database, unique_user):
    asyncio.run(record_tutorial_step_edited(
        actor_user_id=unique_user, auth_principal=unique_user,
        step_id=42, step_slug="welcome", change_kind="update",
        changed_fields=["title", "body"],
    ))
    rows = _events_for_user(database, unique_user, "tutorial_step_edited")
    assert len(rows) == 1
    meta = rows[0]["inputs_meta"]
    assert meta["step_id"] == 42
    assert meta["step_slug"] == "welcome"
    assert meta["change_kind"] == "update"
    assert meta["changed_fields"] == "title,body"
    assert rows[0]["action_type"] == "tutorial_step.update"


def test_record_when_no_recorder_wired_is_silent(database, unique_user):
    set_recorder(None)
    # Should NOT raise even though no recorder is wired
    asyncio.run(record_onboarding_started(
        actor_user_id=unique_user, auth_principal=unique_user, step_slug=None,
    ))
