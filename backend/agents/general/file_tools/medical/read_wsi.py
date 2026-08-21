"""``read_wsi`` tool: whole-slide pathology images (.svs, .ndpi) via OpenSlide."""

from __future__ import annotations

import io
import logging
import os
from typing import Any, Dict, Optional

from agents.general.file_tools import attachment_parser_scope, resolve_attachment
from agents.general.file_tools.medical import _common

logger = logging.getLogger("FileTools.read_wsi")

_PROPS_OF_INTEREST = (
    "openslide.vendor",
    "openslide.objective-power",
    "openslide.mpp-x",
    "openslide.mpp-y",
    "openslide.comment",
    "aperio.AppMag",
    "hamamatsu.SourceLens",
)


@attachment_parser_scope
def read_wsi(
    attachment_id: str,
    user_id: Optional[str] = None,
    **_ignored: Any,
) -> Dict[str, Any]:
    """Return WSI level layout, MPP metadata, and the embedded thumbnail."""
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
        level_dimensions = [list(d) for d in slide.level_dimensions]
        level_downsamples = [float(d) for d in slide.level_downsamples]
        props_all = dict(slide.properties)
        props = {k: props_all[k] for k in _PROPS_OF_INTEREST if k in props_all}
        associated = list(slide.associated_images.keys())

        result: Dict[str, Any] = {
            "filename": att.filename,
            "content_type": "image/tiff",
            "level_count": int(slide.level_count),
            "level_dimensions": level_dimensions,
            "level_downsamples": level_downsamples,
            "properties": props,
            "associated_images": associated,
        }

        try:
            thumb = slide.get_thumbnail((1024, 1024))
            buf = io.BytesIO()
            thumb.save(buf, format="PNG")
            result["thumbnail_png_base64"] = _common.encode_png_b64(buf.getvalue())
            result["thumbnail_content_type"] = "image/png"
            result["thumbnail_width"], result["thumbnail_height"] = thumb.size
        except Exception as exc:
            logger.warning("wsi thumbnail failed: %s", exc)
            result["thumbnail_error"] = str(exc)

        return result
    finally:
        try:
            slide.close()
        except Exception:
            pass


__all__ = ["read_wsi"]
