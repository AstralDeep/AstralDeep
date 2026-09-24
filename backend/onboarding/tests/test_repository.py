"""Tests for onboarding/repository.py: state defaults and upserts, step
listing/ordering/archival, revision writes on create and update including the no-op
case, and idempotent archive/restore.
"""

from __future__ import annotations

import uuid

import pytest

from onboarding.repository import DuplicateSlug, StepNotFound


def _slug(request, suffix: str) -> str:
    return f"pytest-{request.node.name}-{suffix}-{uuid.uuid4().hex[:6]}"


def test_get_state_returns_not_started_default(onboarding_repo, unique_user):
    state = onboarding_repo.get_state(unique_user)
    assert state.status == "not_started"
    assert state.last_step_id is None
    assert state.completed_at is None
    assert state.skipped_at is None


def test_upsert_state_creates_row(onboarding_repo, unique_user):
    new_state, prior = onboarding_repo.upsert_state(unique_user, "in_progress", None)
    assert prior is None
    assert new_state.status == "in_progress"
    assert new_state.started_at is not None
    assert new_state.completed_at is None


def test_upsert_state_completed_sets_completed_at(onboarding_repo, unique_user):
    onboarding_repo.upsert_state(unique_user, "in_progress", None)
    new_state, prior = onboarding_repo.upsert_state(unique_user, "completed", None)
    assert prior == "in_progress"
    assert new_state.status == "completed"
    assert new_state.completed_at is not None


def test_upsert_state_skipped_sets_skipped_at(onboarding_repo, unique_user):
    onboarding_repo.upsert_state(unique_user, "in_progress", None)
    new_state, prior = onboarding_repo.upsert_state(unique_user, "skipped", None)
    assert prior == "in_progress"
    assert new_state.status == "skipped"
    assert new_state.skipped_at is not None


def test_upsert_state_idempotent_on_repeat(onboarding_repo, unique_user):
    onboarding_repo.upsert_state(unique_user, "in_progress", None)
    a, _ = onboarding_repo.upsert_state(unique_user, "in_progress", None)
    b, _ = onboarding_repo.upsert_state(unique_user, "in_progress", None)
    assert a.status == b.status == "in_progress"


def test_list_steps_user_only_excludes_admin(onboarding_repo, request, unique_user):
    user_slug = _slug(request, "u")
    admin_slug = _slug(request, "a")
    onboarding_repo.create_step(
        editor_user_id=unique_user, slug=user_slug, audience="user",
        display_order=10000, target_kind="none", target_key=None,
        title="t", body="b",
    )
    onboarding_repo.create_step(
        editor_user_id=unique_user, slug=admin_slug, audience="admin",
        display_order=10001, target_kind="none", target_key=None,
        title="t", body="b",
    )
    user_steps = onboarding_repo.list_steps_for_user(include_admin=False)
    assert any(s.slug == user_slug for s in user_steps)
    assert not any(s.slug == admin_slug for s in user_steps)

    admin_steps = onboarding_repo.list_steps_for_user(include_admin=True)
    assert any(s.slug == user_slug for s in admin_steps)
    assert any(s.slug == admin_slug for s in admin_steps)


def test_list_steps_excludes_archived(onboarding_repo, request, unique_user):
    slug = _slug(request, "arch")
    dto = onboarding_repo.create_step(
        editor_user_id=unique_user, slug=slug, audience="user",
        display_order=99999, target_kind="none", target_key=None,
        title="t", body="b",
    )
    onboarding_repo.archive_step(step_id=dto.id, editor_user_id=unique_user)
    user_steps = onboarding_repo.list_steps_for_user(include_admin=False)
    assert not any(s.slug == slug for s in user_steps)
    admin_steps = onboarding_repo.list_steps_for_user(include_admin=True)
    assert not any(s.slug == slug for s in admin_steps)


def test_list_steps_ordered_by_display_order(onboarding_repo, request, unique_user):
    later = _slug(request, "z")
    earlier = _slug(request, "a")
    onboarding_repo.create_step(
        editor_user_id=unique_user, slug=later, audience="user",
        display_order=88888, target_kind="none", target_key=None,
        title="t", body="b",
    )
    onboarding_repo.create_step(
        editor_user_id=unique_user, slug=earlier, audience="user",
        display_order=88887, target_kind="none", target_key=None,
        title="t", body="b",
    )
    steps = [s.slug for s in onboarding_repo.list_steps_for_user(include_admin=False)]
    assert steps.index(earlier) < steps.index(later)


def test_get_step_audience_returns_none_for_archived(onboarding_repo, request, unique_user):
    slug = _slug(request, "ga")
    dto = onboarding_repo.create_step(
        editor_user_id=unique_user, slug=slug, audience="admin",
        display_order=99998, target_kind="none", target_key=None,
        title="t", body="b",
    )
    assert onboarding_repo.get_step_audience(dto.id) == "admin"
    onboarding_repo.archive_step(step_id=dto.id, editor_user_id=unique_user)
    assert onboarding_repo.get_step_audience(dto.id) is None


def test_create_step_writes_create_revision(onboarding_repo, request, unique_user):
    slug = _slug(request, "cr")
    dto = onboarding_repo.create_step(
        editor_user_id=unique_user, slug=slug, audience="user",
        display_order=77777, target_kind="static", target_key="some.key",
        title="t", body="b",
    )
    revs = onboarding_repo.list_revisions(dto.id)
    assert len(revs) == 1
    assert revs[0].change_kind == "create"
    assert revs[0].previous is None
    assert revs[0].current["slug"] == slug


def test_duplicate_slug_raises(onboarding_repo, request, unique_user):
    slug = _slug(request, "dup")
    onboarding_repo.create_step(
        editor_user_id=unique_user, slug=slug, audience="user",
        display_order=66666, target_kind="none", target_key=None,
        title="t", body="b",
    )
    with pytest.raises(DuplicateSlug):
        onboarding_repo.create_step(
            editor_user_id=unique_user, slug=slug, audience="user",
            display_order=66667, target_kind="none", target_key=None,
            title="t", body="b",
        )


def test_update_step_minimizes_changed_fields(onboarding_repo, request, unique_user):
    slug = _slug(request, "up")
    dto = onboarding_repo.create_step(
        editor_user_id=unique_user, slug=slug, audience="user",
        display_order=55555, target_kind="none", target_key=None,
        title="Original", body="Body",
    )
    updated, changed = onboarding_repo.update_step(
        step_id=dto.id, editor_user_id=unique_user,
        partial={"title": "New title", "body": "Body"},
    )
    assert "title" in changed
    assert "body" not in changed
    assert updated.title == "New title"


def test_update_step_writes_revision_with_previous(onboarding_repo, request, unique_user):
    slug = _slug(request, "uprev")
    dto = onboarding_repo.create_step(
        editor_user_id=unique_user, slug=slug, audience="user",
        display_order=44444, target_kind="none", target_key=None,
        title="A", body="b",
    )
    onboarding_repo.update_step(
        step_id=dto.id, editor_user_id=unique_user, partial={"title": "B"},
    )
    revs = onboarding_repo.list_revisions(dto.id)
    assert revs[0].change_kind == "update"
    assert revs[0].previous["title"] == "A"
    assert revs[0].current["title"] == "B"


def test_update_step_no_changes_no_revision(onboarding_repo, request, unique_user):
    slug = _slug(request, "noop")
    dto = onboarding_repo.create_step(
        editor_user_id=unique_user, slug=slug, audience="user",
        display_order=33333, target_kind="none", target_key=None,
        title="X", body="Y",
    )
    pre = onboarding_repo.list_revisions(dto.id)
    onboarding_repo.update_step(
        step_id=dto.id, editor_user_id=unique_user, partial={"title": "X"},
    )
    post = onboarding_repo.list_revisions(dto.id)
    assert len(post) == len(pre)


def test_archive_restore_round_trip(onboarding_repo, request, unique_user):
    slug = _slug(request, "ar")
    dto = onboarding_repo.create_step(
        editor_user_id=unique_user, slug=slug, audience="user",
        display_order=22222, target_kind="none", target_key=None,
        title="x", body="y",
    )
    archived = onboarding_repo.archive_step(step_id=dto.id, editor_user_id=unique_user)
    assert archived.archived_at is not None
    restored = onboarding_repo.restore_step(step_id=dto.id, editor_user_id=unique_user)
    assert restored.archived_at is None
    revs = [r.change_kind for r in onboarding_repo.list_revisions(dto.id)]
    assert "archive" in revs
    assert "restore" in revs


def test_archive_idempotent(onboarding_repo, request, unique_user):
    slug = _slug(request, "ai")
    dto = onboarding_repo.create_step(
        editor_user_id=unique_user, slug=slug, audience="user",
        display_order=11111, target_kind="none", target_key=None,
        title="x", body="y",
    )
    onboarding_repo.archive_step(step_id=dto.id, editor_user_id=unique_user)
    pre = onboarding_repo.list_revisions(dto.id)
    onboarding_repo.archive_step(step_id=dto.id, editor_user_id=unique_user)
    post = onboarding_repo.list_revisions(dto.id)
    assert len(pre) == len(post)


def test_step_not_found_raises(onboarding_repo, unique_user):
    with pytest.raises(StepNotFound):
        onboarding_repo.update_step(
            step_id=999999999, editor_user_id=unique_user, partial={"title": "x"},
        )
