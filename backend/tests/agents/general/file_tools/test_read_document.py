"""read_document: PDF (text / OCR / vision fallback), DOCX, RTF, ODT."""

from __future__ import annotations

from unittest.mock import MagicMock

from agents.general.file_tools.read_document import read_document
from conftest import (
    _persist, make_docx, make_odt, make_pdf_blank, make_pdf_with_text, make_rtf,
)


def test_pdf_rasterizer_passes_bytes_to_pdf2image(monkeypatch):
    from agents.general.file_tools import ocr
    import pdf2image

    page = object()
    convert = MagicMock(return_value=[page])
    monkeypatch.setattr(pdf2image, "convert_from_bytes", convert)

    assert ocr.rasterize_pdf(b"%PDF-test", dpi=144, max_pages=3) == [page]
    convert.assert_called_once_with(b"%PDF-test", dpi=144, last_page=3)


def test_read_pdf_with_embedded_text(repo, upload_root):
    aid = _persist(repo, user_id="alice", filename="hello.pdf",
                   category="document", extension="pdf",
                   content_type="application/pdf", upload_root=upload_root,
                   payload=make_pdf_with_text(
                       "Hello PDF world from a test, with enough characters here to clear the OCR threshold."
                   ))
    out = read_document(attachment_id=aid, user_id="alice")
    assert "error" not in out
    assert out["vision_required"] is False
    assert "Hello PDF world" in out["text"]
    assert out["page_count"] == 1


def test_read_pdf_blank_falls_back_to_vision(repo, upload_root, monkeypatch):
    """Blank PDF: embedded extraction yields nothing → vision-model path
    with rasterized page images. Stubbed so the test doesn't require poppler."""
    from agents.general.file_tools import read_document as rd

    def _fake_pdf_to_vision(_path):
        return [{"content_type": "image/png", "image_base64": "ZmFrZQ=="}]

    monkeypatch.setattr(rd, "pdf_to_vision_images", _fake_pdf_to_vision)

    aid = _persist(repo, user_id="alice", filename="blank.pdf",
                   category="document", extension="pdf",
                   content_type="application/pdf", upload_root=upload_root,
                   payload=make_pdf_blank())
    out = read_document(attachment_id=aid, user_id="alice")
    assert "error" not in out
    assert out["vision_required"] is True
    assert out["text"] == ""
    assert len(out["images"]) == 1


def test_read_pdf_blank_no_poppler_returns_empty_images(repo, upload_root, monkeypatch):
    """If rasterization fails (e.g., poppler missing), images is empty but
    the call still succeeds with vision_required=True so the agent can tell
    the user the PDF was unreadable."""
    from agents.general.file_tools import read_document as rd

    monkeypatch.setattr(rd, "pdf_to_vision_images", lambda _p: [])

    aid = _persist(repo, user_id="alice", filename="blank.pdf",
                   category="document", extension="pdf",
                   content_type="application/pdf", upload_root=upload_root,
                   payload=make_pdf_blank())
    out = read_document(attachment_id=aid, user_id="alice")
    assert out["vision_required"] is True
    assert out["images"] == []


def test_read_docx(repo, upload_root):
    aid = _persist(repo, user_id="alice", filename="memo.docx",
                   category="document", extension="docx",
                   content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                   upload_root=upload_root,
                   payload=make_docx(["First paragraph.", "Second paragraph."]))
    out = read_document(attachment_id=aid, user_id="alice")
    assert "error" not in out
    assert "First paragraph." in out["text"]
    assert "Second paragraph." in out["text"]


def test_read_rtf(repo, upload_root):
    aid = _persist(repo, user_id="alice", filename="memo.rtf",
                   category="document", extension="rtf",
                   content_type="application/rtf", upload_root=upload_root,
                   payload=make_rtf("RTF body line one\nRTF body line two"))
    out = read_document(attachment_id=aid, user_id="alice")
    assert "error" not in out
    assert "RTF body" in out["text"]


def test_read_odt(repo, upload_root):
    aid = _persist(repo, user_id="alice", filename="notes.odt",
                   category="document", extension="odt",
                   content_type="application/vnd.oasis.opendocument.text",
                   upload_root=upload_root,
                   payload=make_odt("ODT body content here"))
    out = read_document(attachment_id=aid, user_id="alice")
    assert "error" not in out
    assert "ODT body" in out["text"]


def test_read_document_max_chars_truncates(repo, upload_root):
    aid = _persist(repo, user_id="alice", filename="big.docx",
                   category="document", extension="docx",
                   content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                   upload_root=upload_root,
                   payload=make_docx(["x" * 5000]))
    out = read_document(attachment_id=aid, user_id="alice", max_chars=100)
    assert out["truncated"] is True
    assert len(out["text"]) == 100
