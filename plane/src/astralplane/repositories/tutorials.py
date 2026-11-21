"""Typed persistence for global tutorial steps and their immutable edit-revision
history, seeded by stable slug. Used by AstralDeep's onboarding/repository.py.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from astralplane.contracts import QueryExecutor, Transaction
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
    _bounded_limit,
    _bounded_text,
    _canonical_json,
    _non_negative_int,
    _positive_int,
    _row_value,
    _structured_json,
)

_AUDIENCES = frozenset({"admin", "user"})
_TARGET_KINDS = frozenset({"none", "sdui", "static"})
_EDITABLE_FIELDS = (
    "audience",
    "display_order",
    "target_kind",
    "target_key",
    "title",
    "body",
)
_STEP_FIELDS = """
    id, slug, audience, display_order, target_kind, target_key,
    title, body, created_at, updated_at, archived_at
"""
_REVISION_FIELDS = """
    id, step_id, editor_user_id, edited_at, previous, current, change_kind
"""


@dataclass(frozen=True, slots=True)
class TutorialStepRecord:
    step_id: int
    slug: str
    audience: str
    display_order: int
    target_kind: str
    target_key: str | None
    title: str
    body: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


@dataclass(frozen=True, slots=True)
class TutorialStepRevisionRecord:
    revision_id: int
    step_id: int
    editor_id: str
    edited_at: datetime
    previous: Mapping[str, Any] | None
    current: Mapping[str, Any]
    change_kind: str


@dataclass(frozen=True, slots=True)
class TutorialStepUpdate:
    record: TutorialStepRecord
    changed_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TutorialSeedResult:
    record: TutorialStepRecord
    created: bool


class TutorialRepository:
    def get(self, query: QueryExecutor, *, step_id: int) -> TutorialStepRecord | None:
        row = query.fetch_one(
            f"SELECT {_STEP_FIELDS} FROM tutorial_step WHERE id = %s",
            (_positive_int(step_id, "step_id"),),
        )
        return None if row is None else _step(row)

    def list_visible(
        self,
        query: QueryExecutor,
        *,
        audiences: Sequence[str],
        include_archived: bool = False,
        limit: int = 200,
    ) -> tuple[TutorialStepRecord, ...]:
        selected = _selected_audiences(audiences)
        bounded_limit = _bounded_limit(limit, maximum=500)
        archived_clause = "" if include_archived else "AND archived_at IS NULL"
        rows = query.fetch_all(
            f"""
            SELECT {_STEP_FIELDS} FROM tutorial_step
            WHERE audience = ANY(%s) {archived_clause}
            ORDER BY display_order, id LIMIT %s
            """,
            (list(selected), bounded_limit),
        )
        return tuple(_step(row) for row in rows)

    def list_for_administration(
        self,
        query: QueryExecutor,
        *,
        include_archived: bool = True,
        limit: int = 500,
    ) -> tuple[TutorialStepRecord, ...]:
        bounded_limit = _bounded_limit(limit, maximum=500)
        where = "" if include_archived else "WHERE archived_at IS NULL"
        rows = query.fetch_all(
            f"""
            SELECT {_STEP_FIELDS} FROM tutorial_step
            {where} ORDER BY display_order, id LIMIT %s
            """,
            (bounded_limit,),
        )
        return tuple(_step(row) for row in rows)

    def create_with_revision(
        self,
        transaction: Transaction,
        *,
        slug: str,
        audience: str,
        display_order: int,
        target_kind: str,
        target_key: str | None,
        title: str,
        body: str,
        editor_id: str,
        observed_at: datetime,
    ) -> TutorialStepRecord:
        values = _validated_values(
            slug=slug,
            audience=audience,
            display_order=display_order,
            target_kind=target_kind,
            target_key=target_key,
            title=title,
            body=body,
        )
        editor = _required_editor(editor_id)
        observed = _aware_time(observed_at, "observed_at")
        row = self._insert_if_absent(transaction, values=values, observed_at=observed)
        if row is None:
            existing = transaction.fetch_one(
                f"SELECT {_STEP_FIELDS} FROM tutorial_step WHERE slug = %s",
                (values["slug"],),
            )
            if existing is None:
                raise RepositoryDataError("tutorial insert conflict did not expose its row")
            raise RepositoryConflictError(
                "tutorial slug already exists", metadata={"slug": values["slug"]}
            )
        record = _step(row)
        _insert_revision(
            transaction,
            record=record,
            previous=None,
            editor_id=editor,
            edited_at=observed,
            change_kind="create",
        )
        return record

    def create_seed_if_absent(
        self,
        transaction: Transaction,
        *,
        slug: str,
        audience: str,
        display_order: int,
        target_kind: str,
        target_key: str | None,
        title: str,
        body: str,
        editor_id: str,
        observed_at: datetime,
    ) -> TutorialSeedResult:
        values = _validated_values(
            slug=slug,
            audience=audience,
            display_order=display_order,
            target_kind=target_kind,
            target_key=target_key,
            title=title,
            body=body,
        )
        editor = _required_editor(editor_id)
        observed = _aware_time(observed_at, "observed_at")
        row = self._insert_if_absent(transaction, values=values, observed_at=observed)
        if row is None:
            row = transaction.fetch_one(
                f"SELECT {_STEP_FIELDS} FROM tutorial_step WHERE slug = %s",
                (values["slug"],),
            )
            if row is None:
                raise RepositoryDataError("tutorial seed did not create or locate its slug")
            return TutorialSeedResult(record=_step(row), created=False)
        record = _step(row)
        _insert_revision(
            transaction,
            record=record,
            previous=None,
            editor_id=editor,
            edited_at=observed,
            change_kind="create",
        )
        return TutorialSeedResult(record=record, created=True)

    def update_with_revision(
        self,
        transaction: Transaction,
        *,
        step_id: int,
        expected_updated_at: datetime,
        changes: Mapping[str, object],
        editor_id: str,
        updated_at: datetime,
    ) -> TutorialStepUpdate:
        identifier = _positive_int(step_id, "step_id")
        expected = _aware_time(expected_updated_at, "expected_updated_at")
        changed_at = _aware_time(updated_at, "updated_at")
        if changed_at <= expected:
            raise RepositoryValidationError("updated_at must be later than expected_updated_at")
        editor = _required_editor(editor_id)
        patch = _validated_patch(changes)
        row = transaction.fetch_one(
            f"SELECT {_STEP_FIELDS} FROM tutorial_step WHERE id = %s FOR UPDATE",
            (identifier,),
        )
        if row is None:
            raise RepositoryNotFoundError("tutorial step was not found")
        previous = _step(row)
        if previous.updated_at != expected:
            raise RepositoryConflictError("tutorial step changed since it was read")
        effective = _effective_patch(previous, patch)
        changed_fields = tuple(field for field in _EDITABLE_FIELDS if field in effective)
        if not changed_fields:
            return TutorialStepUpdate(record=previous, changed_fields=())
        _validate_target(
            str(effective.get("target_kind", previous.target_kind)),
            effective.get("target_key", previous.target_key),
        )
        assignments = ", ".join(f"{field} = %s" for field in changed_fields)
        parameters = [effective[field] for field in changed_fields]
        parameters.extend((changed_at, identifier, expected))
        updated = transaction.fetch_one(
            f"""
            UPDATE tutorial_step SET {assignments}, updated_at = %s
            WHERE id = %s AND updated_at = %s
            RETURNING {_STEP_FIELDS}
            """,
            tuple(parameters),
        )
        if updated is None:
            raise RepositoryConflictError("tutorial step compare-and-set failed")
        record = _step(updated)
        _insert_revision(
            transaction,
            record=record,
            previous=previous,
            editor_id=editor,
            edited_at=changed_at,
            change_kind="update",
        )
        return TutorialStepUpdate(record=record, changed_fields=changed_fields)

    def set_archived_with_revision(
        self,
        transaction: Transaction,
        *,
        step_id: int,
        expected_updated_at: datetime,
        archived: bool,
        editor_id: str,
        updated_at: datetime,
    ) -> TutorialStepUpdate:
        if not isinstance(archived, bool):
            raise RepositoryValidationError("archived must be a boolean")
        identifier = _positive_int(step_id, "step_id")
        expected = _aware_time(expected_updated_at, "expected_updated_at")
        changed_at = _aware_time(updated_at, "updated_at")
        if changed_at <= expected:
            raise RepositoryValidationError("updated_at must be later than expected_updated_at")
        editor = _required_editor(editor_id)
        row = transaction.fetch_one(
            f"SELECT {_STEP_FIELDS} FROM tutorial_step WHERE id = %s FOR UPDATE",
            (identifier,),
        )
        if row is None:
            raise RepositoryNotFoundError("tutorial step was not found")
        previous = _step(row)
        if previous.updated_at != expected:
            raise RepositoryConflictError("tutorial step changed since it was read")
        if (previous.archived_at is not None) is archived:
            return TutorialStepUpdate(record=previous, changed_fields=())
        archived_at = changed_at if archived else None
        updated = transaction.fetch_one(
            f"""
            UPDATE tutorial_step SET archived_at = %s, updated_at = %s
            WHERE id = %s AND updated_at = %s
            RETURNING {_STEP_FIELDS}
            """,
            (archived_at, changed_at, identifier, expected),
        )
        if updated is None:
            raise RepositoryConflictError("tutorial archive compare-and-set failed")
        record = _step(updated)
        _insert_revision(
            transaction,
            record=record,
            previous=previous,
            editor_id=editor,
            edited_at=changed_at,
            change_kind="archive" if archived else "restore",
        )
        return TutorialStepUpdate(record=record, changed_fields=("archived_at",))

    def list_revisions(
        self,
        query: QueryExecutor,
        *,
        step_id: int,
        limit: int = 100,
    ) -> tuple[TutorialStepRevisionRecord, ...]:
        rows = query.fetch_all(
            f"""
            SELECT {_REVISION_FIELDS} FROM tutorial_step_revision
            WHERE step_id = %s ORDER BY edited_at DESC, id DESC LIMIT %s
            """,
            (_positive_int(step_id, "step_id"), _bounded_limit(limit, maximum=500)),
        )
        return tuple(_revision(row) for row in rows)

    @staticmethod
    def _insert_if_absent(
        transaction: Transaction,
        *,
        values: Mapping[str, object],
        observed_at: datetime,
    ) -> Mapping[str, Any] | None:
        return transaction.fetch_one(
            f"""
            INSERT INTO tutorial_step (
                slug, audience, display_order, target_kind, target_key,
                title, body, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (slug) DO NOTHING
            RETURNING {_STEP_FIELDS}
            """,
            (
                values["slug"],
                values["audience"],
                values["display_order"],
                values["target_kind"],
                values["target_key"],
                values["title"],
                values["body"],
                observed_at,
                observed_at,
            ),
        )


def _step(row: Mapping[str, Any]) -> TutorialStepRecord:
    created_at = _stored_time(_row_value(row, "created_at"), "created_at")
    updated_at = _stored_time(_row_value(row, "updated_at"), "updated_at")
    archived_at = row.get("archived_at")
    if archived_at is not None:
        archived_at = _stored_time(archived_at, "archived_at")
    record = TutorialStepRecord(
        step_id=int(_row_value(row, "id")),
        slug=str(_row_value(row, "slug")),
        audience=str(_row_value(row, "audience")),
        display_order=int(_row_value(row, "display_order")),
        target_kind=str(_row_value(row, "target_kind")),
        target_key=None if row.get("target_key") is None else str(row["target_key"]),
        title=str(_row_value(row, "title")),
        body=str(_row_value(row, "body")),
        created_at=created_at,
        updated_at=updated_at,
        archived_at=archived_at,
    )
    try:
        _validated_values(
            slug=record.slug,
            audience=record.audience,
            display_order=record.display_order,
            target_kind=record.target_kind,
            target_key=record.target_key,
            title=record.title,
            body=record.body,
        )
    except RepositoryValidationError as exc:
        raise RepositoryDataError("persisted tutorial step is invalid") from exc
    return record


def _revision(row: Mapping[str, Any]) -> TutorialStepRevisionRecord:
    previous_value = row.get("previous")
    previous = (
        None if previous_value is None else _structured_json(previous_value, "previous")
    )
    current = _structured_json(_row_value(row, "current"), "current")
    if (previous is not None and not isinstance(previous, Mapping)) or not isinstance(
        current, Mapping
    ):
        raise RepositoryDataError("persisted tutorial revision snapshots must be objects")
    change_kind = str(_row_value(row, "change_kind"))
    if change_kind not in {"create", "update", "archive", "restore"}:
        raise RepositoryDataError("persisted tutorial revision change kind is invalid")
    return TutorialStepRevisionRecord(
        revision_id=int(_row_value(row, "id")),
        step_id=int(_row_value(row, "step_id")),
        editor_id=str(_row_value(row, "editor_user_id")),
        edited_at=_stored_time(_row_value(row, "edited_at"), "edited_at"),
        previous=previous,
        current=current,
        change_kind=change_kind,
    )


def _insert_revision(
    transaction: Transaction,
    *,
    record: TutorialStepRecord,
    previous: TutorialStepRecord | None,
    editor_id: str,
    edited_at: datetime,
    change_kind: str,
) -> None:
    result = transaction.execute(
        """
        INSERT INTO tutorial_step_revision (
            step_id, editor_user_id, edited_at, previous, current, change_kind
        ) VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s)
        """,
        (
            record.step_id,
            editor_id,
            edited_at,
            None if previous is None else _canonical_json(_snapshot(previous), "previous"),
            _canonical_json(_snapshot(record), "current"),
            change_kind,
        ),
    )
    if result.rowcount != 1:
        raise RepositoryDataError("tutorial revision insert did not affect exactly one row")


def _snapshot(record: TutorialStepRecord) -> dict[str, object]:
    return {
        "id": record.step_id,
        "slug": record.slug,
        "audience": record.audience,
        "display_order": record.display_order,
        "target_kind": record.target_kind,
        "target_key": record.target_key,
        "title": record.title,
        "body": record.body,
        "archived_at": None if record.archived_at is None else record.archived_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


def _validated_values(
    *,
    slug: object,
    audience: object,
    display_order: object,
    target_kind: object,
    target_key: object,
    title: object,
    body: object,
) -> dict[str, object]:
    selected_audience = str(audience)
    if selected_audience not in _AUDIENCES:
        raise RepositoryValidationError("audience is not supported")
    selected_kind = str(target_kind)
    if selected_kind not in _TARGET_KINDS:
        raise RepositoryValidationError("target_kind is not supported")
    normalized_target = (
        None
        if target_key is None
        else _bounded_text(target_key, "target_key", maximum=256)
    )
    _validate_target(selected_kind, normalized_target)
    return {
        "slug": _bounded_text(slug, "slug", maximum=128),
        "audience": selected_audience,
        "display_order": _non_negative_int(display_order, "display_order"),
        "target_kind": selected_kind,
        "target_key": normalized_target,
        "title": _bounded_text(title, "title", maximum=120),
        "body": _bounded_text(body, "body", maximum=1000),
    }


def _validated_patch(changes: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(changes, Mapping):
        raise RepositoryValidationError("changes must be a mapping")
    unknown = set(changes).difference(_EDITABLE_FIELDS)
    if unknown:
        raise RepositoryValidationError(
            "changes contains unsupported fields", metadata={"fields": sorted(unknown)}
        )
    patch: dict[str, object] = {}
    for field, value in changes.items():
        if field == "audience":
            if value not in _AUDIENCES:
                raise RepositoryValidationError("audience is not supported")
        elif field == "display_order":
            value = _non_negative_int(value, "display_order")
        elif field == "target_kind":
            if value not in _TARGET_KINDS:
                raise RepositoryValidationError("target_kind is not supported")
        elif field == "target_key":
            value = None if value is None else _bounded_text(value, field, maximum=256)
        elif field == "title":
            value = _bounded_text(value, field, maximum=120)
        elif field == "body":
            value = _bounded_text(value, field, maximum=1000)
        patch[field] = value
    return patch


def _effective_patch(
    previous: TutorialStepRecord, patch: Mapping[str, object]
) -> dict[str, object]:
    return {
        field: value
        for field, value in patch.items()
        if value != getattr(previous, field)
    }


def _selected_audiences(audiences: Sequence[str]) -> tuple[str, ...]:
    if isinstance(audiences, (str, bytes)) or not audiences or len(audiences) > 2:
        raise RepositoryValidationError("audiences must contain one or two values")
    selected = tuple(dict.fromkeys(audiences))
    if set(selected).difference(_AUDIENCES):
        raise RepositoryValidationError("audiences contains an unsupported value")
    return selected


def _validate_target(target_kind: str, target_key: object) -> None:
    if target_kind == "none" and target_key is not None:
        raise RepositoryValidationError("target_key must be absent for target_kind none")
    if target_kind != "none" and target_key is None:
        raise RepositoryValidationError("target_key is required for a targeted tutorial step")


def _required_editor(editor_id: object) -> str:
    return _bounded_text(editor_id, "editor_id", maximum=512)


def _aware_time(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RepositoryValidationError(f"{field} must be a timezone-aware datetime")
    return value


def _stored_time(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RepositoryDataError(
            "persisted timestamp is not timezone-aware", metadata={"field": field}
        )
    return value


__all__ = (
    "TutorialRepository",
    "TutorialSeedResult",
    "TutorialStepRecord",
    "TutorialStepRevisionRecord",
    "TutorialStepUpdate",
)
