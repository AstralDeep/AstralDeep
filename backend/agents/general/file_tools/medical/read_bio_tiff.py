"""``read_bio_tiff`` tool: parse OME-TIFF / bio-TIFF files via tifffile."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import numpy as np

from agents.general.file_tools import attachment_parser_scope, resolve_attachment
from agents.general.file_tools.medical import _common

logger = logging.getLogger("FileTools.read_bio_tiff")


@attachment_parser_scope
def read_bio_tiff(
    attachment_id: str,
    user_id: Optional[str] = None,
    **_ignored: Any,
) -> Dict[str, Any]:
    """Return series/level layout + thumbnail for an OME-TIFF or generic TIFF."""
    att, path, err = resolve_attachment(attachment_id, user_id)
    if err is not None:
        return err

    try:
        import tifffile  # type: ignore
    except Exception as exc:
        return _common.missing_dep("tifffile", exc)

    try:
        tf = tifffile.TiffFile(os.fspath(path))
    except Exception as exc:
        logger.exception("tiff open failed")
        return _common.error("parse_failed", f"Failed to read TIFF: {exc}")

    try:
        is_ome = bool(tf.is_ome)
    except Exception:
        is_ome = False
    try:
        is_bigtiff = bool(tf.is_bigtiff)
    except Exception:
        is_bigtiff = False

    series_info: List[Dict[str, Any]] = []
    for idx, ser in enumerate(tf.series):
        try:
            series_info.append({
                "index": idx,
                "name": getattr(ser, "name", None) or f"series_{idx}",
                "shape": list(ser.shape),
                "dtype": str(ser.dtype),
                "axes": getattr(ser, "axes", None),
                "levels": len(getattr(ser, "levels", []) or [ser]),
            })
        except Exception:
            continue

    ome_xml: Optional[str] = None
    if is_ome:
        try:
            ome_xml = tf.ome_metadata
        except Exception:
            ome_xml = None

    result: Dict[str, Any] = {
        "filename": att.filename,
        "content_type": "image/tiff",
        "is_ome": is_ome,
        "is_bigtiff": is_bigtiff,
        "series": series_info,
        "ome_xml_preview": (ome_xml[:2000] + "…") if ome_xml and len(ome_xml) > 2000 else ome_xml,
    }

    # Thumbnail: series 0, deepest pyramid level for speed.
    try:
        ser0 = tf.series[0]
        levels = getattr(ser0, "levels", None) or [ser0]
        thumb_level = levels[-1]
        arr = thumb_level.asarray()
        if arr.ndim > 2:
            # Reduce extra axes by picking middle index of each until 2-D or 2-D+channels.
            while arr.ndim > 3:
                arr = arr[arr.shape[0] // 2]
            if arr.ndim == 3 and arr.shape[-1] not in (1, 3, 4) and arr.shape[0] not in (1, 3, 4):
                arr = arr[arr.shape[0] // 2]
        result["pixel_stats"] = _common.basic_stats(np.asarray(arr))
        result.update(_common.thumbnail_field(arr))
    except Exception as exc:
        logger.warning("tiff thumbnail failed: %s", exc)
        result["thumbnail_error"] = str(exc)
    finally:
        try:
            tf.close()
        except Exception:
            pass

    return result


__all__ = ["read_bio_tiff"]
