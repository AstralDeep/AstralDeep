"""extract_wsi_region tool: crops a rectangular region (capped at 2048x2048) from a
whole-slide pathology image (.svs/.ndpi) via OpenSlide, returned as a base64 PNG.
"""

from __future__ import annotations

import io
import logging
import os
from typing import Any, Dict, Optional

from agents.general.file_tools import attachment_parser_scope, resolve_attachment
from agents.general.file_tools.medical import _common

logger = logging.getLogger("FileTools.extract_wsi_region")

_MAX_REGION_PX = 2048


# x/y are level-0 coords; width/height are the requested level
@attachment_parser_scope
def extract_wsi_region(
    attachment_id: str,
    user_id: Optional[str] = None,
    level: int = 0,
    x: int = 0,
    y: int = 0,
    width: int = 512,
    height: int = 512,
    **_ignored: Any,
) -> Dict[str, Any]:
    att, path, err = resolve_attachment(attachment_id, user_id)
    if err is not None:
        return err

    try:
        import openslide  # type: ignore
    except Exception as exc:
        return _common.missing_dep("openslide", exc)

    try:
        slide = openslide.OpenSlide(os.fspath(path))
    except Exception as exc:
        logger.exception("wsi open failed")
        return _common.error("parse_failed", f"Failed to open WSI: {exc}")

    try:
        if level < 0 or level >= slide.level_count:
            return _common.error(
                "parse_failed",
                f"level {level} out of range [0, {slide.level_count - 1}].",
            )

        w = max(1, min(int(width), _MAX_REGION_PX))
        h = max(1, min(int(height), _MAX_REGION_PX))

        try:
            region = slide.read_region((int(x), int(y)), int(level), (w, h))
        except Exception as exc:
            return _common.error("parse_failed", f"read_region failed: {exc}")

        buf = io.BytesIO()
        region.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        return {
            "filename": att.filename,
            "level": int(level),
            "x": int(x),
            "y": int(y),
            "width": w,
            "height": h,
            "content_type": "image/png",
            "image_base64": _common.encode_png_b64(png_bytes),
        }
    finally:
        try:
            slide.close()
        except Exception:
            pass


__all__ = ["extract_wsi_region"]
