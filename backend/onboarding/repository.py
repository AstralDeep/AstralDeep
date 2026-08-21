"""Product-facing repository facade for the onboarding subsystem.

Every query that touches ``onboarding_state``, ``tutorial_step``, or
``tutorial_step_revision`` lives here. The router and recorder NEVER
write SQL inline — they go through this module.

Design notes:

* User-scoped operations (``get_state``, ``upsert_state``,
  ``list_steps_for_user``) take ``actor_user_id`` / ``include_admin`` as
  explicit parameters so the API layer cannot accidentally widen the
  scope. There is no "list all" helper for ``onboarding_state`` because
  no caller has a legitimate cross-user use case (mirrors feature 003's
  audit-repository policy).
* Admin write operations (``create_step``, ``update_step``,
  ``archive_step``, ``restore_step``) bundle the canonical-table mutation
  with the matching ``tutorial_step_revision`` row inside a single DB
  transaction. The audit-log emit happens at the recorder layer, after
  the transaction commits, so a partial DB failure cannot leak an audit
  row that doesn't reflect a real change.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from astralplane.repositories import RepositoryConflictError, RepositoryNotFoundError
from astralplane.repositories.preferences import OnboardingStateRecord
from astralplane.repositories.tutorials import (
    TutorialStepRecord,
    TutorialStepRevisionRecord,
)
from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)

from .schemas import (
    OnboardingStateResponse,
    RevisionDTO,
    TutorialStepDTO,
)

logger = logging.getLogger("Onboarding.Repository")


def _after(previous: datetime) -> datetime:
    return max(datetime.now(timezone.utc), previous + timedelta(microseconds=1))


class StepNotFound(Exception):
    """Raised when an admin write targets a non-existent step."""


class DuplicateSlug(Exception):
    """Raised when an admin attempts to create a step with an in-use slug."""


class OnboardingRepository:
    """Thin façade over the three feature-005 tables."""

    def __init__(
        self,
        db: Any,
        *,
        plane_runtime=None,
        plane_repositories=None,
        preferences_repository=None,
    ):
        preferences, runtime = repository_from(
            "preferences",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=db,
        )
        self._state = PlaneRepositoryContext(
            repository=(preferences_repository or preferences).onboarding,
            plane_runtime=runtime,
            legacy_database=db,
        )
        tutorials, tutorial_runtime = repository_from(
            "tutorials",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=db,
        )
        self._tutorials = PlaneRepositoryContext(
            repository=tutorials,
            plane_runtime=tutorial_runtime,
            legacy_database=db,
        )

    # ------------------------------------------------------------------
    # Onboarding state
    # ------------------------------------------------------------------

    def get_state(self, user_id: str) -> OnboardingStateResponse:
        """Return the user's onboarding state, defaulting to ``not_started``.

        Absence of a row maps to the implicit default; this is the only
        place we materialize that default so callers never need to.
        """
        record = self._state.call(
            self._state.repository.get_state,
            owner_id=user_id,
        )
        return self._state_response(record)

    def upsert_state(
        self,
        user_id: str,
        status: str,
        last_step_id: Optional[int],
    ) -> Tuple[OnboardingStateResponse, Optional[str]]:
        """Insert or update the user's row.

        Returns ``(new_state, prior_status)`` where ``prior_status`` is
        ``None`` when no row existed before this call. The caller (the
        recorder) uses ``prior_status`` to decide which audit event to
        record.
        """
        with self._state.transaction() as transaction:
            existing = self._state.repository.get_state(
                transaction,
                owner_id=user_id,
            )
            prior_status = None if existing is None else existing.status
            if existing is None:
                now = datetime.now(timezone.utc)
                candidate = OnboardingStateRecord(
                    owner_id=user_id,
                    status=status,
                    last_step_id=last_step_id,
                    started_at=now,
                    updated_at=now,
                    completed_at=now if status == "completed" else None,
                    skipped_at=now if status == "skipped" else None,
                    dismissed_at=None,
                    dismiss_count=0,
                )
                expected_updated_at = None
            else:
                now = _after(existing.updated_at)
                candidate = OnboardingStateRecord(
                    owner_id=user_id,
                    status=status,
                    last_step_id=last_step_id,
                    started_at=existing.started_at,
                    updated_at=now,
                    completed_at=(
                        existing.completed_at
                        or (now if status == "completed" else None)
                    ),
                    skipped_at=(
                        existing.skipped_at or (now if status == "skipped" else None)
                    ),
                    dismissed_at=existing.dismissed_at,
                    dismiss_count=existing.dismiss_count,
                )
                expected_updated_at = existing.updated_at
            durable = self._state.repository.put_state(
                transaction,
                candidate,
                expected_updated_at=expected_updated_at,
            )
        return self._state_response(durable), prior_status

    def record_dismissal(self, user_id: str, max_dismissals: int = 2) -> OnboardingStateResponse:
        """Record a 'not now' dismissal.

        Increments dismiss_count and sets dismissed_at. If dismiss_count
        reaches max_dismissals, auto-transitions status to 'skipped' so
        the tour stops prompting.
        """
        with self._state.transaction() as transaction:
            existing = self._state.repository.get_state(
                transaction,
                owner_id=user_id,
            )
            now = (
                datetime.now(timezone.utc)
                if existing is None
                else _after(existing.updated_at)
            )
            new_count = 1 if existing is None else existing.dismiss_count + 1
            auto_skip = new_count >= max_dismissals
            candidate = OnboardingStateRecord(
                owner_id=user_id,
                status=(
                    "skipped"
                    if auto_skip
                    else ("not_started" if existing is None else existing.status)
                ),
                last_step_id=None if existing is None else existing.last_step_id,
                started_at=now if existing is None else existing.started_at,
                updated_at=now,
                completed_at=None if existing is None else existing.completed_at,
                skipped_at=(
                    now
                    if auto_skip and (existing is None or existing.skipped_at is None)
                    else (None if existing is None else existing.skipped_at)
                ),
                dismissed_at=now,
                dismiss_count=new_count,
            )
            durable = self._state.repository.put_state(
                transaction,
                candidate,
                expected_updated_at=(
                    None if existing is None else existing.updated_at
                ),
            )
        return self._state_response(durable)

    def _state_response(
        self,
        record: OnboardingStateRecord | None,
    ) -> OnboardingStateResponse:
        if record is None:
            return OnboardingStateResponse(status="not_started")
        step = None if record.last_step_id is None else self.get_step(record.last_step_id)
        return OnboardingStateResponse(
            status=record.status,
            last_step_id=record.last_step_id,
            last_step_slug=None if step is None else step.slug,
            started_at=record.started_at,
            completed_at=record.completed_at,
            skipped_at=record.skipped_at,
            dismissed_at=record.dismissed_at,
            dismiss_count=record.dismiss_count,
        )

    # ------------------------------------------------------------------
    # Tutorial steps — read paths
    # ------------------------------------------------------------------

    def list_steps_for_user(self, *, include_admin: bool) -> List[TutorialStepDTO]:
        """Return the ordered, non-archived steps the caller can see.

        The caller passes ``include_admin=True`` only after verifying the
        admin role in the API layer.
        """
        if include_admin:
            audiences = ("user", "admin")
        else:
            audiences = ("user",)
        records = self._tutorials.call(
            self._tutorials.repository.list_visible,
            audiences=audiences,
            include_archived=False,
            limit=200,
        )
        return [_tutorial_step_to_dto(record) for record in records]

    def list_all_steps(self, include_archived: bool = True) -> List[TutorialStepDTO]:
        """Admin read: returns every step, optionally including archived ones."""
        records = self._tutorials.call(
            self._tutorials.repository.list_for_administration,
            include_archived=include_archived,
            limit=500,
        )
        return [_tutorial_step_to_dto(record) for record in records]

    def get_step(self, step_id: int) -> Optional[TutorialStepDTO]:
        record = self._tutorials.call(
            self._tutorials.repository.get,
            step_id=step_id,
        )
        return None if record is None else _tutorial_step_to_dto(record)

    def get_step_audience(self, step_id: int) -> Optional[str]:
        """Return just the audience for a step. Used for cheap validation."""
        record = self._tutorials.call(
            self._tutorials.repository.get,
            step_id=step_id,
        )
        if record is None or record.archived_at is not None:
            return None
        return record.audience

    # ------------------------------------------------------------------
    # Tutorial steps — admin write paths
    # ------------------------------------------------------------------

    def create_step(
        self,
        *,
        editor_user_id: str,
        slug: str,
        audience: str,
        display_order: int,
        target_kind: str,
        target_key: Optional[str],
        title: str,
        body: str,
    ) -> TutorialStepDTO:
        try:
            with self._tutorials.transaction() as transaction:
                record = self._tutorials.repository.create_with_revision(
                    transaction,
                    slug=slug,
                    audience=audience,
                    display_order=display_order,
                    target_kind=target_kind,
                    target_key=target_key,
                    title=title,
                    body=body,
                    editor_id=editor_user_id,
                    observed_at=datetime.now(timezone.utc),
                )
        except RepositoryConflictError as exc:
            raise DuplicateSlug(slug) from exc
        return _tutorial_step_to_dto(record)

    def update_step(
        self,
        *,
        step_id: int,
        editor_user_id: str,
        partial: Dict[str, Any],
    ) -> Tuple[TutorialStepDTO, List[str]]:
        """Apply a partial update.

        ``partial`` may contain any of: audience, display_order, target_kind,
        target_key, title, body. Only fields present in the dict (i.e.
        keys whose value is set, including ``None`` for ``target_key``)
        are written.

        Returns ``(updated_dto, changed_fields)`` where ``changed_fields``
        is the list of column names whose values actually changed (no
        false positives).
        """
        if not partial:
            existing = self.get_step(step_id)
            if existing is None:
                raise StepNotFound(step_id)
            return existing, []

        allowed = ("audience", "display_order", "target_kind", "target_key", "title", "body")
        unknown = set(partial.keys()) - set(allowed)
        if unknown:
            raise ValueError(f"cannot update unknown fields: {sorted(unknown)}")

        existing = self._tutorials.call(
            self._tutorials.repository.get,
            step_id=step_id,
        )
        if existing is None:
            raise StepNotFound(step_id)
        try:
            with self._tutorials.transaction() as transaction:
                result = self._tutorials.repository.update_with_revision(
                    transaction,
                    step_id=step_id,
                    expected_updated_at=existing.updated_at,
                    changes=partial,
                    editor_id=editor_user_id,
                    updated_at=_after(existing.updated_at),
                )
        except RepositoryNotFoundError as exc:
            raise StepNotFound(step_id) from exc
        return _tutorial_step_to_dto(result.record), list(result.changed_fields)

    def archive_step(self, *, step_id: int, editor_user_id: str) -> TutorialStepDTO:
        return self._toggle_archive(step_id=step_id, editor_user_id=editor_user_id, archive=True)

    def restore_step(self, *, step_id: int, editor_user_id: str) -> TutorialStepDTO:
        return self._toggle_archive(step_id=step_id, editor_user_id=editor_user_id, archive=False)

    def _toggle_archive(self, *, step_id: int, editor_user_id: str, archive: bool) -> TutorialStepDTO:
        existing = self._tutorials.call(
            self._tutorials.repository.get,
            step_id=step_id,
        )
        if existing is None:
            raise StepNotFound(step_id)
        try:
            with self._tutorials.transaction() as transaction:
                result = self._tutorials.repository.set_archived_with_revision(
                    transaction,
                    step_id=step_id,
                    expected_updated_at=existing.updated_at,
                    archived=archive,
                    editor_id=editor_user_id,
                    updated_at=_after(existing.updated_at),
                )
        except RepositoryNotFoundError as exc:
            raise StepNotFound(step_id) from exc
        return _tutorial_step_to_dto(result.record)

    def list_revisions(self, step_id: int) -> List[RevisionDTO]:
        records = self._tutorials.call(
            self._tutorials.repository.list_revisions,
            step_id=step_id,
            limit=500,
        )
        return [_tutorial_revision_to_dto(record) for record in records]


# ---------------------------------------------------------------------------
# Helpers — Plane record → DTO
# ---------------------------------------------------------------------------

def _tutorial_step_to_dto(record: TutorialStepRecord) -> TutorialStepDTO:
    return TutorialStepDTO(
        id=record.step_id,
        slug=record.slug,
        audience=record.audience,
        display_order=record.display_order,
        target_kind=record.target_kind,
        target_key=record.target_key,
        title=record.title,
        body=record.body,
        archived_at=record.archived_at,
        updated_at=record.updated_at,
    )


def _tutorial_revision_to_dto(record: TutorialStepRevisionRecord) -> RevisionDTO:
    return RevisionDTO(
        id=record.revision_id,
        step_id=record.step_id,
        editor_user_id=record.editor_id,
        edited_at=record.edited_at,
        change_kind=record.change_kind,
        previous=None if record.previous is None else dict(record.previous),
        current=dict(record.current),
    )
