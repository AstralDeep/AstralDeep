"""Tests for orchestrator/attachment_autoparse.py's coverage_status decisions: built-in
or globally live types report covered, pending registry rows dedup, flag-off reports
unavailable, and uncovered types report preparing.
"""

from __future__ import annotations

import types

from orchestrator import attachment_autoparse, parser_registry
from shared.feature_flags import flags


class _ParserRepository:
    rows: dict[str, dict] = {}

    def __init__(self, _db, **_kwargs):
        pass

    @classmethod
    def from_plane_source(cls, _source):
        return cls(None)

    def get_by_gap(self, fingerprint):
        return self.rows.get(fingerprint)


def _orch():
    return types.SimpleNamespace(
        runtime_composition=types.SimpleNamespace(
            plane=types.SimpleNamespace(runtime=object(), repositories=object())
        )
    )


def _bind_repository(monkeypatch, rows=None):
    from orchestrator.attachments import parser_repo

    _ParserRepository.rows = dict(rows or {})
    monkeypatch.setattr(
        parser_repo,
        "AttachmentParserRepository",
        _ParserRepository,
    )


def test_builtin_covered_type_reports_covered(monkeypatch):
    _bind_repository(monkeypatch)
    out = attachment_autoparse.coverage_status(_orch(), extension="pdf", category="document")
    assert out["status"] == "covered"


def test_uncovered_type_reports_preparing(monkeypatch):
    _bind_repository(monkeypatch)
    monkeypatch.setitem(flags._flags, "attachment_autoparse", True)
    out = attachment_autoparse.coverage_status(_orch(), extension="parquet", category="data")
    assert out["status"] == "preparing"
    assert out["gap_fingerprint"] == parser_registry.gap_fingerprint("data", "parquet")


def test_pending_registry_row_reports_pending_admin_approval(monkeypatch):
    monkeypatch.setitem(flags._flags, "attachment_autoparse", True)
    fp = parser_registry.gap_fingerprint("archive", "zip")
    _bind_repository(monkeypatch, {fp: {"status": "pending"}})
    out = attachment_autoparse.coverage_status(_orch(), extension="zip", category="archive")
    assert out["status"] == "pending_admin_approval"


def test_live_registry_row_reports_covered(monkeypatch):
    monkeypatch.setitem(flags._flags, "attachment_autoparse", True)
    fp = parser_registry.gap_fingerprint("data", "avro")
    _bind_repository(
        monkeypatch,
        {fp: {"status": "live", "tool_name": "parse_avro"}},
    )
    out = attachment_autoparse.coverage_status(_orch(), extension="avro", category="data")
    assert out["status"] == "covered"


def test_flag_off_reports_unavailable(monkeypatch):
    _bind_repository(monkeypatch)
    monkeypatch.setitem(flags._flags, "attachment_autoparse", False)
    out = attachment_autoparse.coverage_status(_orch(), extension="parquet", category="data")
    assert out["status"] == "unavailable"


def test_failed_registry_row_allows_reattempt(monkeypatch):
    monkeypatch.setitem(flags._flags, "attachment_autoparse", True)
    fp = parser_registry.gap_fingerprint("data", "orc")
    _bind_repository(monkeypatch, {fp: {"status": "failed"}})
    out = attachment_autoparse.coverage_status(_orch(), extension="orc", category="data")
    assert out["status"] == "preparing"


def test_tool_name_is_identifier_safe():
    assert attachment_autoparse._tool_name_for("nii.gz") == "parse_nii_gz"
    assert attachment_autoparse._tool_name_for("7z") == "parse_7z"
    assert attachment_autoparse._tool_name_for(None) == "parse_file"
