"""Verify onboarding storage through AstralPlane's current schema contract."""

from __future__ import annotations

import pytest

from astralplane.database.baseline import BASELINE_REQUIRED_TABLES
from astralplane.database.migrations import (
    CURRENT_DATA_PLANE_REVISION,
    MIGRATION_REGISTRY,
)


def _table_exists(database, name: str) -> bool:
    with database.transaction() as transaction:
        return (
            transaction.fetch_one(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = %s",
                (name,),
            )
            is not None
        )


def _column_exists(database, table: str, column: str) -> bool:
    with database.transaction() as transaction:
        return (
            transaction.fetch_one(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = %s AND column_name = %s",
                (table, column),
            )
            is not None
        )


def _index_exists(database, name: str) -> bool:
    with database.transaction() as transaction:
        return (
            transaction.fetch_one(
                "SELECT 1 FROM pg_indexes "
                "WHERE schemaname = current_schema() AND indexname = %s",
                (name,),
            )
            is not None
        )


def test_plane_contract_owns_onboarding_tables() -> None:
    assert CURRENT_DATA_PLANE_REVISION.schema_revision == "074.004"
    assert CURRENT_DATA_PLANE_REVISION.migration_digest == MIGRATION_REGISTRY.digest
    assert {
        "onboarding_state",
        "tutorial_step",
        "tutorial_step_revision",
    } <= BASELINE_REQUIRED_TABLES


def test_tables_exist(database):
    assert _table_exists(database, "onboarding_state")
    assert _table_exists(database, "tutorial_step")
    assert _table_exists(database, "tutorial_step_revision")


def test_onboarding_state_columns(database):
    for column in (
        "user_id",
        "status",
        "last_step_id",
        "started_at",
        "updated_at",
        "completed_at",
        "skipped_at",
    ):
        assert _column_exists(database, "onboarding_state", column), column


def test_tutorial_step_columns(database):
    for column in (
        "id",
        "slug",
        "audience",
        "display_order",
        "target_kind",
        "target_key",
        "title",
        "body",
        "created_at",
        "updated_at",
        "archived_at",
    ):
        assert _column_exists(database, "tutorial_step", column), column


def test_tutorial_step_revision_columns(database):
    for column in (
        "id",
        "step_id",
        "editor_user_id",
        "edited_at",
        "previous",
        "current",
        "change_kind",
    ):
        assert _column_exists(database, "tutorial_step_revision", column), column


def test_indexes_exist(database):
    assert _index_exists(database, "idx_tutorial_step_user_view")
    assert _index_exists(database, "idx_tutorial_step_revision_step_time")
    assert _index_exists(database, "idx_tutorial_step_revision_editor")


def test_status_check_constraint(database):
    with pytest.raises(Exception), database.transaction() as transaction:
        transaction.execute(
            "INSERT INTO onboarding_state (user_id, status) VALUES (%s, %s)",
            ("pytest-bad-status", "totally_invalid"),
        )


def test_target_consistency_constraint(database):
    with pytest.raises(Exception), database.transaction() as transaction:
        transaction.execute(
            """
            INSERT INTO tutorial_step (
                slug, audience, display_order, target_kind, target_key, title, body
            ) VALUES (
                'pytest-bad-target', 'user', 999, 'none',
                'should-not-have-key', 'T', 'B'
            )
            """
        )
