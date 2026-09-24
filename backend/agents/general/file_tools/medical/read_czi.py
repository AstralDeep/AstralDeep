"""read_czi tool: parses Zeiss .czi microscopy files via aicspylibczi, returning
dimension metadata plus a mid-plane thumbnail.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import numpy as np

from agents.general.file_tools import attachment_parser_scope, resolve_attachment
from agents.general.file_tools.medical import _common

logger = logging.getLogger("FileTools.read_czi")


def _flatten_to_2d(arr: np.ndarray) -> np.ndarray:
    while arr.ndim > 2:
        # aicspylibczi axes are S,T,C,Z,Y,X; middle-index each extra one
        idx = arr.shape[0] // 2
        arr = arr[idx]
    return arr


@attachment_parser_scope
def read_czi(
    attachment_id: str,
    user_id: Optional[str] = None,
    scene: int = 0,
    **_ignored: Any,
) -> Dict[str, Any]:
    att, path, err = resolve_attachment(attachment_id, user_id)
    if err is not None:
        return err

    try:
        from aicspylibczi import CziFile  # type: ignore
    except Exception as exc:
        return _common.missing_dep("aicspylibczi", exc)

    try:
        czi = CziFile(os.fspath(path))
    except Exception as exc:
        logger.exception("czi open failed")
        return _common.error("parse_failed", f"Failed to read CZI: {exc}")

    try:
        dims_shape = czi.get_dims_shape()
    except Exception:
        dims_shape = None
    try:
        dims_str = czi.dims
    except Exception:
        dims_str = None
    try:
        pixel_type = str(czi.pixel_type)
    except Exception:
        pixel_type = None
    try:
        is_mosaic = bool(czi.is_mosaic())
    except Exception:
        is_mosaic = None

    result: Dict[str, Any] = {
        "filename": att.filename,
        "content_type": "application/octet-stream",
        "dims": dims_str,
        "dims_shape": dims_shape,
        "pixel_type": pixel_type,
        "is_mosaic": is_mosaic,
        "scene": scene,
    }

    try:
        read_kwargs: Dict[str, Any] = {}
        if "S" in (dims_str or ""):
            read_kwargs["S"] = scene
        if "C" in (dims_str or ""):
            read_kwargs["C"] = 0
        if dims_shape:
            first = dims_shape[0]
            if "Z" in first:
                z_min, z_max = first["Z"]
                read_kwargs["Z"] = (z_min + z_max - 1) // 2
        tile, _shape = czi.read_image(**read_kwargs)
        flat = _flatten_to_2d(np.asarray(tile))
        result["pixel_stats"] = _common.basic_stats(flat)
        result.update(_common.thumbnail_field(flat))
    except Exception as exc:
        logger.warning("czi thumbnail failed: %s", exc)
        result["thumbnail_error"] = str(exc)

    return result


__all__ = ["read_czi"]
