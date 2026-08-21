"""``compute_volume_statistics`` tool: histogram + projections for a volume."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import numpy as np

from agents.general.file_tools import attachment_parser_scope, resolve_attachment
from agents.general.file_tools.medical import _common
from agents.general.file_tools.medical.extract_volume_slice import _load_volume

logger = logging.getLogger("FileTools.compute_volume_statistics")


@attachment_parser_scope
def compute_volume_statistics(
    attachment_id: str,
    user_id: Optional[str] = None,
    bins: int = 64,
    **_ignored: Any,
) -> Dict[str, Any]:
    """Return an intensity histogram and per-axis MIP thumbnails for a volume."""
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

    arr = np.asarray(vol)
    while arr.ndim > 3:
        arr = arr[0]

    stats = _common.basic_stats(arr)

    bins = max(2, min(int(bins), 512))
    hist_bins: list = []
    hist_counts: list = []
    try:
        finite = arr[np.isfinite(arr)] if np.issubdtype(arr.dtype, np.floating) else arr
        if finite.size > 0:
            counts, edges = np.histogram(finite, bins=bins)
            hist_counts = [int(c) for c in counts]
            hist_bins = [float(e) for e in edges]
    except Exception as exc:
        logger.warning("histogram failed: %s", exc)

    result: Dict[str, Any] = {
        "filename": att.filename,
        "source": source,
        "pixel_stats": stats,
        "histogram": {"bin_edges": hist_bins, "counts": hist_counts},
    }

    if arr.ndim == 3:
        try:
            # Max-intensity projection along each axis — good for the LLM to
            # see structure at a glance.
            result["mip_axial"] = _common.thumbnail_field(arr.max(axis=0))
            result["mip_coronal"] = _common.thumbnail_field(arr.max(axis=1))
            result["mip_sagittal"] = _common.thumbnail_field(arr.max(axis=2))
        except Exception as exc:
            logger.warning("MIP projections failed: %s", exc)
            result["projection_error"] = str(exc)

    return result


__all__ = ["compute_volume_statistics"]
