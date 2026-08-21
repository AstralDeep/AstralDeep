"""``read_nifti`` tool: parse .nii / .nii.gz volumes with nibabel."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from agents.general.file_tools import attachment_parser_scope, resolve_attachment
from agents.general.file_tools.medical import _common

logger = logging.getLogger("FileTools.read_nifti")


@attachment_parser_scope
def read_nifti(
    attachment_id: str,
    user_id: Optional[str] = None,
    **_ignored: Any,
) -> Dict[str, Any]:
    """Return NIfTI header info plus three orthogonal mid-plane thumbnails."""
    att, path, err = resolve_attachment(attachment_id, user_id)
    if err is not None:
        return err

    try:
        import nibabel as nib  # type: ignore
        import numpy as np  # noqa: F401  (imported by _common but surface the error here too)
    except Exception as exc:
        return _common.missing_dep("nibabel", exc)

    try:
        img = nib.load(os.fspath(path))
        data = img.get_fdata()
        header = img.header
    except Exception as exc:
        logger.exception("nifti parse failed")
        return _common.error("parse_failed", f"Failed to read NIfTI: {exc}")

    try:
        orientation = "".join(nib.orientations.aff2axcodes(img.affine))
    except Exception:
        orientation = None

    try:
        zooms = [float(z) for z in header.get_zooms()]
    except Exception:
        zooms = []

    try:
        affine = img.affine.tolist()
    except Exception:
        affine = None

    result: Dict[str, Any] = {
        "filename": att.filename,
        "content_type": "application/octet-stream",
        "shape": list(data.shape),
        "dtype": str(data.dtype),
        "voxel_sizes": zooms,
        "orientation": orientation,
        "affine": affine,
        "pixel_stats": _common.basic_stats(data),
    }

    if data.ndim >= 3:
        try:
            axial = _common.middle_slice(data, axis=2)
            coronal = _common.middle_slice(data, axis=1)
            sagittal = _common.middle_slice(data, axis=0)
            result["axial"] = _common.thumbnail_field(axial)
            result["coronal"] = _common.thumbnail_field(coronal)
            result["sagittal"] = _common.thumbnail_field(sagittal)
        except Exception as exc:
            logger.warning("nifti thumbnails failed: %s", exc)
            result["thumbnail_error"] = str(exc)
    elif data.ndim == 2:
        try:
            result.update(_common.thumbnail_field(data))
        except Exception as exc:
            result["thumbnail_error"] = str(exc)

    return result


__all__ = ["read_nifti"]
