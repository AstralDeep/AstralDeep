"""read_volume_itk tool: parses NRRD and MetaImage (.mha/.mhd) volumes via SimpleITK,
returning header info plus a middle-slice thumbnail.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from agents.general.file_tools import attachment_parser_scope, resolve_attachment
from agents.general.file_tools.medical import _common

logger = logging.getLogger("FileTools.read_volume_itk")


@attachment_parser_scope
def read_volume_itk(
    attachment_id: str,
    user_id: Optional[str] = None,
    **_ignored: Any,
) -> Dict[str, Any]:
    att, path, err = resolve_attachment(attachment_id, user_id)
    if err is not None:
        return err

    try:
        import SimpleITK as sitk  # type: ignore
        import numpy as np  # noqa: F401
    except Exception as exc:
        return _common.missing_dep("SimpleITK", exc)

    try:
        img = sitk.ReadImage(os.fspath(path))
    except Exception as exc:
        logger.exception("itk read failed")
        return _common.error("parse_failed", f"Failed to read volume: {exc}")

    try:
        # SimpleITK→numpy reverses axes: array is z,y,x
        arr = sitk.GetArrayFromImage(img)
    except Exception as exc:
        return _common.error("parse_failed", f"Failed to extract pixel data: {exc}")

    meta_keys = []
    try:
        meta_keys = list(img.GetMetaDataKeys())
    except Exception:
        pass

    meta: Dict[str, Any] = {}
    for key in meta_keys:
        try:
            meta[key] = img.GetMetaData(key)
        except Exception:
            continue

    result: Dict[str, Any] = {
        "filename": att.filename,
        "content_type": "application/octet-stream",
        "size": list(img.GetSize()),
        "spacing": [float(s) for s in img.GetSpacing()],
        "origin": [float(o) for o in img.GetOrigin()],
        "direction": [float(d) for d in img.GetDirection()],
        "pixel_type": str(img.GetPixelIDTypeAsString()),
        "components_per_pixel": int(img.GetNumberOfComponentsPerPixel()),
        "metadata": meta,
        "pixel_stats": _common.basic_stats(arr),
    }

    try:
        if arr.ndim >= 3:
            slice_2d = _common.middle_slice(arr, axis=0)
        else:
            slice_2d = arr
        result.update(_common.thumbnail_field(slice_2d))
    except Exception as exc:
        logger.warning("itk thumbnail failed: %s", exc)
        result["thumbnail_error"] = str(exc)

    return result


__all__ = ["read_volume_itk"]
