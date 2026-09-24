"""Resolves whether an attachment type is parseable today, checking built-in category
readers and admin-promoted registry rows. Consulted by orchestrator.py and
attachment_autoparse.py before triggering auto-parser creation.
"""

from __future__ import annotations

import hashlib
from typing import Optional, TypedDict

BUILTIN_CATEGORY_TOOL = {
    "document": "read_document",
    "spreadsheet": "read_spreadsheet",
    "presentation": "read_presentation",
    "text": "read_text",
    "image": "read_image",
    "medical": "read_dicom",
}


class Coverage(TypedDict):
    covered: bool
    tool: Optional[str]
    source: Optional[str]


def gap_fingerprint(category: str, extension: Optional[str]) -> str:
    ext = (extension or "").lower().strip()
    cat = (category or "").lower().strip()
    digest = hashlib.sha256(f"attachment_parser:{cat}:{ext}".encode()).hexdigest()
    return digest[:32]


def builtin_tool_for(category: str) -> Optional[str]:
    return BUILTIN_CATEGORY_TOOL.get((category or "").lower())


def coverage(
    extension: Optional[str],
    category: str,
    *,
    parser_repo=None,
) -> Coverage:
    builtin = builtin_tool_for(category)
    if builtin is not None:
        return {"covered": True, "tool": builtin, "source": "builtin"}
    if parser_repo is not None:
        fp = gap_fingerprint(category, extension)
        row = parser_repo.get_by_gap(fp)
        if row and row.get("status") == "live" and row.get("tool_name"):
            return {"covered": True, "tool": row["tool_name"], "source": "global"}
    return {"covered": False, "tool": None, "source": None}


def is_covered(extension: Optional[str], category: str, *, parser_repo=None) -> bool:
    return coverage(extension, category, parser_repo=parser_repo)["covered"]


def covering_tool(extension: Optional[str], category: str, *, parser_repo=None) -> Optional[str]:
    return coverage(extension, category, parser_repo=parser_repo)["tool"]


__all__ = [
    "BUILTIN_CATEGORY_TOOL",
    "Coverage",
    "builtin_tool_for",
    "covering_tool",
    "coverage",
    "gap_fingerprint",
    "is_covered",
]
