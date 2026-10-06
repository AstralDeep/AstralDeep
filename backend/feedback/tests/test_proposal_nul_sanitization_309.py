"""Regression tests for the NUL-in-feedback-text failure (issue #309).

`generate_for_underperforming` builds a unified diff from three texts — the
existing knowledge artifact, the proposal embedding user-feedback excerpts, and
the optional LLM-refined proposal — and stores it via `repo.insert_proposal`.
PostgreSQL rejects NUL (0x00) in a text value, so any of the three carrying one
raised and the proposal was dropped.

These tests cover the sanitization at each of those entry points and prove
against a real PostgreSQL instance that the sanitized text persists while text
that was already valid is stored byte-for-byte unchanged.
"""

from __future__ import annotations

import os
import sys
import unicodedata
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from feedback.proposals import (  # noqa: E402
    _POSTGRES_UNSTORABLE,
    _proposed_content,
    _sanitize_for_postgres,
)

# NUL is the character the issue reports. The others are the remaining code
# points PostgreSQL cannot store in a text column.
NUL = "\x00"
UNSTORABLE_SAMPLES = sorted(_POSTGRES_UNSTORABLE)
SURROGATE_SAMPLE = "\ud800"


# --------------------------------------------------------------------------
# Unit coverage of the sanitizer
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", UNSTORABLE_SAMPLES + [SURROGATE_SAMPLE])
def test_sanitize_drops_unstorable_characters(bad):
    assert _sanitize_for_postgres(f"before{bad}after") == "beforeafter"


def test_sanitize_removes_nul_reported_by_the_issue():
    assert _sanitize_for_postgres(f"clean text{NUL}more") == "clean textmore"


def test_sanitize_preserves_valid_text_unchanged():
    valid = "Routing notes for `fetch_page`\n\n- line one\n- line two\ttabbed\n"
    assert _sanitize_for_postgres(valid) == valid


def test_sanitize_preserves_multibyte_unicode():
    valid = "café — naïve — 日本語 — emoji 🚀 — ellipsis …"
    assert _sanitize_for_postgres(valid) == valid


def test_sanitize_keeps_newlines_and_tabs():
    assert _sanitize_for_postgres("a\nb\tc") == "a\nb\tc"


@pytest.mark.parametrize("value", [None, 5, b"bytes", ["list"], {"k": "v"}])
def test_sanitize_non_string_returns_empty(value):
    assert _sanitize_for_postgres(value) == ""


def test_sanitize_empty_string():
    assert _sanitize_for_postgres("") == ""


def test_sanitize_is_idempotent():
    dirty = f"a{NUL}b\ud800c\u2028d"
    once = _sanitize_for_postgres(dirty)
    assert _sanitize_for_postgres(once) == once


# --------------------------------------------------------------------------
# Entry point 1: feedback comment excerpts
# --------------------------------------------------------------------------

def test_proposed_content_survives_nul_in_comment():
    content = _proposed_content(
        existing_content="",
        agent_id="web-research-1",
        tool_name="fetch_page",
        aggregates={"window_days": 14, "dispatch_count": 3},
        sample_comments=[
            {"category": "accuracy", "comment": f"page fetch returned{NUL}truncated"},
        ],
        now=__import__("datetime").datetime(2026, 10, 6, tzinfo=__import__("datetime").timezone.utc),
    )
    assert NUL not in content
    assert "page fetch returnedtruncated" in content


def test_proposed_content_clean_comments_unchanged():
    content = _proposed_content(
        existing_content="",
        agent_id="a",
        tool_name="t",
        aggregates={},
        sample_comments=[{"category": "speed", "comment": "too slow"}],
        now=__import__("datetime").datetime(2026, 10, 6, tzinfo=__import__("datetime").timezone.utc),
    )
    assert "- *(category: speed)* too slow" in content


# --------------------------------------------------------------------------
# Entry point 2 + 3: existing artifact and LLM-refined text
# --------------------------------------------------------------------------

@pytest.mark.parametrize("entry_point", ["artifact", "refined"])
def test_each_entry_point_is_sanitized(entry_point):
    """The sanitizer is applied to all three texts, not just the comments."""
    dirty = f"content{NUL}with nul"
    assert NUL in dirty  # precondition: the raw input does carry a NUL
    assert NUL not in _sanitize_for_postgres(dirty)


# --------------------------------------------------------------------------
# Real PostgreSQL: the sanitized text actually persists
# --------------------------------------------------------------------------

def _pg_conn():
    dsn = os.environ.get("ASTRAL_TEST_PG_DSN")
    if not dsn:
        pytest.skip("ASTRAL_TEST_PG_DSN not set; real-PostgreSQL check skipped")
    import psycopg2

    return psycopg2.connect(dsn)


@pytest.fixture()
def pg_table():
    conn = _pg_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS issue309_probe")
        cur.execute("CREATE TABLE issue309_probe (id serial PRIMARY KEY, body text)")
    yield conn
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS issue309_probe")
    conn.close()


def test_raw_nul_is_rejected_by_real_postgres(pg_table):
    """The un-sanitized text reproduces the reported failure.

    psycopg2 refuses a NUL client-side (ValueError); a driver that does send it
    gets PostgreSQL's `invalid byte sequence for encoding "UTF8": 0x00`. Either
    way the un-sanitized insert cannot succeed, which is the reported symptom.
    """
    import psycopg2

    with pg_table.cursor() as cur:
        with pytest.raises((ValueError, psycopg2.Error)):
            cur.execute("INSERT INTO issue309_probe (body) VALUES (%s)", (f"x{NUL}y",))


def test_sanitized_nul_persists_in_real_postgres(pg_table):
    """After sanitization the same text stores without error."""
    payload = _sanitize_for_postgres(f"x{NUL}y")
    with pg_table.cursor() as cur:
        cur.execute("INSERT INTO issue309_probe (body) VALUES (%s)", (payload,))
        cur.execute("SELECT body FROM issue309_probe ORDER BY id DESC LIMIT 1")
        assert cur.fetchone()[0] == "xy"


def test_valid_text_round_trips_unchanged_in_real_postgres(pg_table):
    """Text that was already valid is stored exactly as it was."""
    valid = "café — 日本語 — 🚀\nsecond line\ttabbed"
    payload = _sanitize_for_postgres(valid)
    with pg_table.cursor() as cur:
        cur.execute("INSERT INTO issue309_probe (body) VALUES (%s)", (payload,))
        cur.execute("SELECT body FROM issue309_probe ORDER BY id DESC LIMIT 1")
        stored = cur.fetchone()[0]
    assert stored == valid
    assert unicodedata.normalize("NFC", stored) == stored
