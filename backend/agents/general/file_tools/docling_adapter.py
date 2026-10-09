"""Docling document adapter: provides opt-in structured parsing for PDF and DOCX attachments.
Preserves table structures, layout reading order, and provenance data with graceful fallback.
"""

from __future__ import annotations

import io
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("FileTools.docling_adapter")

# Opt-in environment flag name
DOCLING_OPT_IN_ENV = "ENABLE_DOCLING_PARSER"


def is_docling_enabled() -> bool:
    val = os.environ.get(DOCLING_OPT_IN_ENV, "").strip().lower()
    return val in ("1", "true", "yes", "on", "enabled")


def parse_structured_document(
    payload: bytes,
    extension: str,
    page_range: Optional[str] = None,
    max_chars: int = 200_000,
) -> Dict[str, Any]:
    if not is_docling_enabled():
        return {
            "status": "disabled",
            "text": "",
            "tables": [],
            "reading_order": [],
            "provenance": {},
            "fallback_required": True,
        }

    try:
        from docling.document_converter import DocumentConverter  # type: ignore
        from docling.datamodel.base_models import DocumentStream  # type: ignore
    except ImportError as exc:
        logger.info(f"Docling not installed or unavailable: {exc}")
        return {
            "status": "import_failed",
            "error_detail": str(exc),
            "text": "",
            "tables": [],
            "reading_order": [],
            "provenance": {},
            "fallback_required": True,
        }

    try:
        converter = DocumentConverter()
        stream = DocumentStream(name=f"document.{extension}", stream=io.BytesIO(payload))
        res = converter.convert(stream)
        doc = getattr(res, "document", None) or res

        tables: List[Dict[str, Any]] = []
        if hasattr(doc, "tables"):
            for t in doc.tables:
                table_dict = {
                    "num_rows": getattr(t, "num_rows", 0),
                    "num_cols": getattr(t, "num_cols", 0),
                    "data": getattr(t, "data", []),
                }
                if hasattr(t, "export_to_dataframe"):
                    table_dict["markdown"] = t.export_to_dataframe().to_markdown()
                tables.append(table_dict)

        text = ""
        if hasattr(doc, "export_to_markdown"):
            text = doc.export_to_markdown()
        elif hasattr(doc, "text"):
            text = str(doc.text)

        truncated = False
        if len(text) > max_chars:
            text = text[:max_chars]
            truncated = True

        return {
            "status": "success",
            "text": text,
            "tables": tables,
            "truncated": truncated,
            "reading_order": getattr(doc, "reading_order", []),
            "provenance": getattr(doc, "provenance", {}),
            "fallback_required": False,
        }
    except Exception as exc:
        logger.warning(f"Docling structured extraction failed, falling back: {exc}")
        return {
            "status": "failed",
            "error_detail": str(exc),
            "text": "",
            "tables": [],
            "reading_order": [],
            "provenance": {},
            "fallback_required": True,
        }
