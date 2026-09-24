"""Rasterizes PDF pages to images and base64-encodes them for the vision-fallback path
used when embedded text extraction is too sparse; consumed by read_document.py. Named
ocr.py for import stability though it performs no OCR.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from PIL import Image

logger = logging.getLogger("FileTools.PDFImages")


def rasterize_pdf(pdf_bytes: bytes, *, dpi: int = 200, max_pages: int = 50) -> List["Image.Image"]:
    try:
        from pdf2image import convert_from_bytes  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"pdf2image unavailable: {exc}")
    images = convert_from_bytes(pdf_bytes, dpi=dpi, last_page=max_pages)
    return list(images)


def encode_images_for_vision(images: List["Image.Image"], *, fmt: str = "PNG") -> List[dict]:
    out: List[dict] = []
    for img in images:
        buf = io.BytesIO()
        img.save(buf, format=fmt)
        out.append({
            "content_type": f"image/{fmt.lower()}",
            "image_base64": base64.b64encode(buf.getvalue()).decode(),
        })
    return out


def pdf_to_vision_images(pdf_bytes: bytes) -> List[dict]:
    try:
        images = rasterize_pdf(pdf_bytes)
    except RuntimeError as exc:
        logger.warning(f"PDF rasterization failed: {exc}")
        return []
    return encode_images_for_vision(images)


__all__ = [
    "encode_images_for_vision",
    "pdf_to_vision_images",
    "rasterize_pdf",
]
