"""Tests for docling_adapter.py and read_document.py integration: verifies opt-in dispatch,
fallback mechanics, structured table/layout extraction, and error handling.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

from agents.general.file_tools.docling_adapter import (
    DOCLING_OPT_IN_ENV,
    is_docling_enabled,
    parse_structured_document,
)
from agents.general.file_tools.read_document import read_document


def test_is_docling_enabled(monkeypatch):
    monkeypatch.delenv(DOCLING_OPT_IN_ENV, raising=False)
    assert not is_docling_enabled()

    monkeypatch.setenv(DOCLING_OPT_IN_ENV, "true")
    assert is_docling_enabled()

    monkeypatch.setenv(DOCLING_OPT_IN_ENV, "1")
    assert is_docling_enabled()

    monkeypatch.setenv(DOCLING_OPT_IN_ENV, "0")
    assert not is_docling_enabled()


def test_parse_structured_document_disabled(monkeypatch):
    monkeypatch.setenv(DOCLING_OPT_IN_ENV, "0")
    res = parse_structured_document(b"sample bytes", "pdf")
    assert res["status"] == "disabled"
    assert res["fallback_required"] is True


def test_parse_structured_document_import_error(monkeypatch):
    monkeypatch.setenv(DOCLING_OPT_IN_ENV, "1")
    with patch.dict(sys.modules, {"docling": None, "docling.document_converter": None}):
        res = parse_structured_document(b"sample bytes", "pdf")
        assert res["status"] in ("import_failed", "failed")
        assert res["fallback_required"] is True


def test_parse_structured_document_success(monkeypatch):
    monkeypatch.setenv(DOCLING_OPT_IN_ENV, "1")

    mock_table = MagicMock()
    mock_table.num_rows = 2
    mock_table.num_cols = 2
    mock_table.data = [["A", "B"], ["1", "2"]]

    mock_doc = MagicMock()
    mock_doc.tables = [mock_table]
    mock_doc.export_to_markdown.return_value = "# Header\nSome content"
    mock_doc.reading_order = ["header_1", "paragraph_1"]
    mock_doc.provenance = {"pages": [1]}

    mock_converter_instance = MagicMock()
    mock_converter_instance.convert.return_value.document = mock_doc

    mock_docling = MagicMock()
    mock_docling.document_converter.DocumentConverter = MagicMock(return_value=mock_converter_instance)
    mock_docling.datamodel.base_models.DocumentStream = MagicMock()

    with patch.dict(sys.modules, {
        "docling": mock_docling,
        "docling.document_converter": mock_docling.document_converter,
        "docling.datamodel": mock_docling.datamodel,
        "docling.datamodel.base_models": mock_docling.datamodel.base_models,
    }):
        res = parse_structured_document(b"%PDF-mock", "pdf")
        assert res["status"] == "success"
        assert not res["fallback_required"]
        assert len(res["tables"]) == 1
        assert res["text"] == "# Header\nSome content"
        assert res["reading_order"] == ["header_1", "paragraph_1"]


def test_read_document_with_docling_fallback(monkeypatch):
    monkeypatch.setenv(DOCLING_OPT_IN_ENV, "1")

    mock_att = MagicMock()
    mock_att.filename = "test.pdf"
    mock_att.content_type = "application/pdf"
    mock_att.extension = "pdf"

    with patch("agents.general.file_tools.read_document.read_attachment_bytes", return_value=(mock_att, b"dummy", None)), \
         patch("agents.general.file_tools.read_document.parse_structured_document", return_value={"fallback_required": True}), \
         patch("agents.general.file_tools.read_document._read_pdf", return_value={"text": "baseline fallback text", "truncated": False}):
        out = read_document("att-123", user_id="alice")
        assert out["filename"] == "test.pdf"
        assert out["text"] == "baseline fallback text"
