"""Feature 031 — idempotent guarded migrations (FR-025).

Asserts the new tables/column exist after _init_db and that re-running _init_db
is safe (idempotent). Requires a real Postgres (the suite runs inside the
astraldeep container against the postgres service); skipped where unreachable.
"""

from __future__ import annotations

import pytest

try:
    import psycopg2  # noqa: F401
    from shared.database import Database
except Exception:  # pragma: no cover - import guard
    Database = None  # type: ignore


def _db_or_skip():
    if Database is None:
        pytest.skip("psycopg2/shared.database unavailable")
    try:
        return Database()
    except Exception as exc:  # pragma: no cover - no DB in this env
        pytest.skip(f"database unreachable: {exc}")


def _table_exists(db, name: str) -> bool:
    row = db.fetch_one("SELECT to_regclass(?) AS t", (name,))
    return bool(row and row["t"])


def test_new_tables_and_column_exist():
    db = _db_or_skip()
    assert _table_exists(db, "message_attachment")
    assert _table_exists(db, "attachment_parser")
    row = db.fetch_one(
        "SELECT 1 AS ok FROM information_schema.columns "
        "WHERE table_name = ? AND column_name = ?",
        ("draft_agents", "source_attachment_id"),
    )
    assert row and row["ok"] == 1


def test_init_db_is_idempotent():
    db = _db_or_skip()
    # Re-running schema init must not raise (guards: IF NOT EXISTS / _column_exists).
    db._init_db()
    db._init_db()
    assert _table_exists(db, "attachment_parser")


def test_attachment_parser_gap_is_unique():
    db = _db_or_skip()
    row = db.fetch_one(
        "SELECT indexname FROM pg_indexes WHERE indexname = ?",
        ("uq_attachment_parser_gap",),
    )
    assert row is not None


def test_message_attachment_read_paths_are_indexed():
    db = _db_or_skip()
    names = {
        r["indexname"]
        for r in db.fetch_all(
            "SELECT indexname FROM pg_indexes WHERE tablename = ?",
            ("message_attachment",),
        )
    }
    # chat-scoped bulk read (list_for_chat) and per-message read
    # (list_for_message / history._attachments) each need their own index.
    assert "idx_message_attachment_chat" in names
    assert "idx_message_attachment_message" in names
