"""``read_document`` tool: PDF, DOCX, RTF, ODT.

Per ``contracts/agent-tools.md``. PDFs that lack extractable text are
rasterized and the page images are returned for the vision-capable model
to interpret directly. There is no OCR step.
"""

from __future__ import annotations

import io
import logging
from typing import Any, Dict, Optional

from agents.general.file_tools import read_attachment_bytes
from agents.general.file_tools.ocr import pdf_to_vision_images

logger = logging.getLogger("FileTools.read_document")

# If embedded extraction yields fewer than this many characters total, we
# treat the PDF as image-only and hand the rasterized pages to the vision
# model (no OCR step).
_TEXT_MIN_CHARS = 32


def _parse_page_range(spec: str, total: int) -> list[int]:
    """Parse '1-3,5,9-' into a list of 0-indexed page numbers."""
    if not spec:
        return list(range(total))
    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            start = int(a) if a else 1
            end = int(b) if b else total
        else:
            start = end = int(part)
        for p in range(max(1, start), min(total, end) + 1):
            pages.append(p - 1)
    return pages


def _read_pdf(payload: bytes, page_range: Optional[str], max_chars: int) -> Dict[str, Any]:
    from pypdf import PdfReader  # type: ignore

    reader = PdfReader(io.BytesIO(payload))
    pages = _parse_page_range(page_range or "", len(reader.pages))
    chunks = []
    for i in pages:
        try:
            chunks.append(reader.pages[i].extract_text() or "")
        except Exception as exc:  # pragma: no cover
            logger.warning(f"page {i} extract failed: {exc}")
    text = "\n".join(c.strip() for c in chunks if c).strip()
    truncated = False
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True

    if len(text) >= _TEXT_MIN_CHARS:
        return {
            "page_count": len(reader.pages),
            "text": text,
            "truncated": truncated,
            "vision_required": False,
            "images": [],
        }

    # Embedded extraction yielded too little — hand pages to the vision model.
    images = pdf_to_vision_images(payload)
    return {
        "page_count": len(reader.pages),
        "text": "",
        "truncated": False,
        "vision_required": True,
        "images": images,
    }


def _read_docx(payload: bytes, max_chars: int) -> Dict[str, Any]:
    import docx  # python-docx

    doc = docx.Document(io.BytesIO(payload))
    text = "\n".join(p.text for p in doc.paragraphs)
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return {"text": text, "truncated": truncated, "vision_required": False, "images": []}


def _read_rtf(payload: bytes, max_chars: int) -> Dict[str, Any]:
    from striprtf.striprtf import rtf_to_text

    raw = payload.decode("utf-8", errors="replace")
    text = rtf_to_text(raw) or ""
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return {"text": text, "truncated": truncated, "vision_required": False, "images": []}


def _read_odt(payload: bytes, max_chars: int) -> Dict[str, Any]:
    from odf.opendocument import load  # type: ignore
    from odf import text as odftext  # type: ignore
    from odf.element import Text  # type: ignore

    doc = load(io.BytesIO(payload))
    parts: list[str] = []

    def _walk(node):
        for child in getattr(node, "childNodes", []):
            if isinstance(child, Text):
                parts.append(child.data)
            else:
                _walk(child)

    for para in doc.getElementsByType(odftext.P):
        _walk(para)
        parts.append("\n")
    text = "".join(parts).strip()
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return {"text": text, "truncated": truncated, "vision_required": False, "images": []}


def read_document(
    attachment_id: str,
    page_range: Optional[str] = None,
    max_chars: int = 200_000,
    user_id: Optional[str] = None,
    **_ignored: Any,
) -> Dict[str, Any]:
    """Read a document attachment (PDF/DOCX/RTF/ODT) and return its text."""
    att, payload, err = read_attachment_bytes(attachment_id, user_id)
    if err is not None:
        return err

    base = {
        "filename": att.filename,
        "content_type": att.content_type,
    }
    try:
        if att.extension == "pdf":
            base.update(_read_pdf(payload, page_range, max_chars))
        elif att.extension == "docx":
            base.update(_read_docx(payload, max_chars))
        elif att.extension == "rtf":
            base.update(_read_rtf(payload, max_chars))
        elif att.extension == "odt":
            base.update(_read_odt(payload, max_chars))
        else:
            return {"error": {
                "code": "unsupported",
                "message": f"read_document does not support .{att.extension}",
            }}
    except Exception as exc:
        logger.exception("document parse failed")
        return {"error": {"code": "parse_failed", "message": str(exc)}}
    return base


__all__ = ["read_document"]
