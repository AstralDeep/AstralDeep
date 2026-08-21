"""``read_text`` tool: TXT, MD, JSON, YAML, XML, HTML, LOG, code."""

from __future__ import annotations

import html
import logging
import re
from html.parser import HTMLParser
from typing import Any, Dict, Optional

from agents.general.file_tools import read_attachment_bytes

logger = logging.getLogger("FileTools.read_text")


_LANGUAGE_BY_EXTENSION = {
    "py": "python", "js": "javascript", "ts": "typescript",
    "tsx": "typescript", "jsx": "javascript", "sql": "sql",
    "sh": "bash", "ps1": "powershell", "css": "css",
    "json": "json", "yaml": "yaml", "yml": "yaml", "xml": "xml",
    "html": "html", "htm": "html", "md": "markdown", "txt": "text",
    "log": "text",
}


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8", "utf-16"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    # Latin-1 maps every byte value and therefore cannot fail.
    return raw.decode("latin-1")


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._skip == 0:
            self._chunks.append(data)

    def text(self) -> str:
        return re.sub(r"\s+", " ", "".join(self._chunks)).strip()


def _strip_html(raw: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(raw)
    return html.unescape(parser.text())


def _strip_xml(raw: str) -> str:
    from defusedxml import ElementTree as ET  # type: ignore

    try:
        root = ET.fromstring(raw)
    except Exception:
        return ""
    parts: list[str] = []
    for el in root.iter():
        if el.text:
            parts.append(el.text.strip())
    return "\n".join(p for p in parts if p)


def read_text(
    attachment_id: str,
    max_chars: int = 200_000,
    user_id: Optional[str] = None,
    **_ignored: Any,
) -> Dict[str, Any]:
    """Read a text-class attachment and return its source plus a plaintext rendering."""
    att, payload, err = read_attachment_bytes(attachment_id, user_id)
    if err is not None:
        return err
    assert att is not None and payload is not None
    text = _decode(payload)

    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]

    plaintext = None
    if att.extension in ("html", "htm"):
        plaintext = _strip_html(text)
    elif att.extension == "xml":
        plaintext = _strip_xml(text)

    return {
        "filename": att.filename,
        "content_type": att.content_type,
        "language": _LANGUAGE_BY_EXTENSION.get(att.extension, "text"),
        "text": text,
        "plaintext": plaintext,
        "truncated": truncated,
    }


__all__ = ["read_text"]
