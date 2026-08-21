"""``extract_volume_slice`` tool: render a single plane from a volumetric file.

Works for DICOM (multi-frame or single), NIfTI, NRRD / MHA / MHD, and volumetric
OME-TIFF. Returns a base64 PNG plus the index that was actually used.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import numpy as np

from agents.general.file_tools import attachment_parser_scope, resolve_attachment
from agents.general.file_tools.medical import _common

logger = logging.getLogger("FileTools.extract_volume_slice")

_AXIS_MAP = {"x": 0, "y": 1, "z": 2, "0": 0, "1": 1, "2": 2}


def _parse_axis(axis: Any) -> int:
    if isinstance(axis, int):
        return max(0, min(2, axis))
    if isinstance(axis, str):
        key = axis.lower()
        if key in _AXIS_MAP:
            return _AXIS_MAP[key]
    return 2  # default: z


def _load_volume(path_str: str, extension: str):
    """Return a numpy volume + source type string, or raise."""
    ext = extension.lower()

    if ext in ("dcm", "dicom"):
        import pydicom  # type: ignore
        ds = pydicom.dcmread(path_str, force=True)
        return np.asarray(ds.pixel_array), "dicom"

    if ext in ("nii", "nii.gz"):
        import nibabel as nib  # type: ignore
        return np.asarray(nib.load(path_str).get_fdata()), "nifti"

    if ext in ("nrrd", "mha", "mhd"):
        import SimpleITK as sitk  # type: ignore
        return np.asarray(sitk.GetArrayFromImage(sitk.ReadImage(path_str))), "itk"

    if ext in ("tif", "tiff", "ome.tif", "ome.tiff"):
        import tifffile  # type: ignore
        return np.asarray(tifffile.imread(path_str)), "tiff"

    raise ValueError(f"Unsupported extension for volume slicing: .{ext}")


@attachment_parser_scope
def extract_volume_slice(
    attachment_id: str,
    user_id: Optional[str] = None,
    axis: Any = "z",
    index: Optional[int] = None,
    **_ignored: Any,
) -> Dict[str, Any]:
    """Render one slice of a 3-D volume as a PNG.

    Args:
        attachment_id: UUID of the uploaded file.
        user_id: Injected by the orchestrator.
        axis: "x" / "y" / "z" or 0 / 1 / 2. Defaults to "z".
        index: 0-based slice index along *axis*. Defaults to the middle.
    """
    att, path, err = resolve_attachment(attachment_id, user_id)
    if err is not None:
        return err

    try:
        vol, source = _load_volume(os.fspath(path), att.extension)
    except ValueError as exc:
        return _common.error("unsupported_file", str(exc))
    except ImportError as exc:
        return _common.missing_dep("volume reader", exc)
    except Exception as exc:
        logger.exception("volume load failed")
        return _common.error("parse_failed", f"Failed to load volume: {exc}")

    while vol.ndim > 3:
        vol = vol[0]

    if vol.ndim < 3:
        # Degenerate 2-D file — just render it.
        try:
            result = {
                "filename": att.filename,
                "source": source,
                "axis_used": None,
                "index_used": None,
                "shape": list(vol.shape),
            }
            result.update(_common.thumbnail_field(vol))
            return result
        except Exception as exc:
            return _common.error("parse_failed", f"Thumbnail failed: {exc}")

    ax = _parse_axis(axis)
    size_along = vol.shape[ax]
    if index is None:
        idx = size_along // 2
    else:
        try:
            idx = int(index)
        except Exception:
            return _common.error("parse_failed", f"index must be an integer; got {index!r}")
        if idx < 0 or idx >= size_along:
            return _common.error(
                "parse_failed",
                f"index {idx} out of range [0, {size_along - 1}] for axis {ax}.",
            )

    slice_2d = np.take(vol, idx, axis=ax)

    try:
        result: Dict[str, Any] = {
            "filename": att.filename,
            "source": source,
            "axis_used": ax,
            "index_used": idx,
            "shape": list(vol.shape),
            "slice_shape": list(slice_2d.shape),
        }
        result.update(_common.thumbnail_field(slice_2d))
        return result
    except Exception as exc:
        return _common.error("parse_failed", f"Thumbnail failed: {exc}")


__all__ = ["extract_volume_slice"]
