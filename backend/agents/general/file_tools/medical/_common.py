"""Shared helpers for the medical-imaging readers: builds an 8-bit PNG thumbnail for the
vision model and summarizes an n-d array's distribution
(min/max/mean/std/shape/dtype).
"""

from __future__ import annotations

import base64
import io
from typing import Any, Dict, Optional, Tuple

import numpy as np

_MAX_THUMBNAIL_DIM = 1024


def basic_stats(array: Any) -> Dict[str, Any]:
    arr = np.asarray(array)
    out: Dict[str, Any] = {
        "shape": list(arr.shape),
        "dtype": str(arr.dtype),
        "size": int(arr.size),
    }
    if arr.size == 0:
        return out
    finite = arr[np.isfinite(arr)] if np.issubdtype(arr.dtype, np.floating) else arr
    if finite.size == 0:
        return out
    out.update({
        "min": float(np.min(finite)),
        "max": float(np.max(finite)),
        "mean": float(np.mean(finite)),
        "std": float(np.std(finite)),
    })
    return out


def middle_slice(volume: Any, axis: int = -1) -> np.ndarray:
    arr = np.asarray(volume)
    while arr.ndim > 3:
        arr = arr[0]
    if arr.ndim < 3:
        return arr
    if axis < 0:
        axis = arr.ndim + axis
    mid = arr.shape[axis] // 2
    return np.take(arr, mid, axis=axis)


def render_thumbnail(
    array_2d: Any,
    max_dim: int = _MAX_THUMBNAIL_DIM,
) -> Tuple[bytes, int, int]:
    from PIL import Image

    arr = np.asarray(array_2d)
    if arr.ndim == 3 and arr.shape[-1] not in (1, 3, 4):
        arr = np.moveaxis(arr[:3] if arr.shape[0] >= 3 else arr[:1], 0, -1)
    if arr.ndim == 3 and arr.shape[-1] == 1:
        arr = arr[..., 0]

    arr = arr.astype(np.float32)
    finite_mask = np.isfinite(arr)
    if not finite_mask.any():
        norm = np.full(arr.shape[:2], 128, dtype=np.uint8)
    else:
        finite = arr[finite_mask]
        lo, hi = float(finite.min()), float(finite.max())
        if hi - lo < 1e-12:
            norm = np.zeros_like(arr, dtype=np.uint8)
        else:
            scaled = (arr - lo) / (hi - lo)
            scaled = np.clip(scaled, 0.0, 1.0)
            scaled[~finite_mask] = 0.0
            norm = (scaled * 255).astype(np.uint8)

    if norm.ndim == 3 and norm.shape[-1] == 4:
        img = Image.fromarray(norm, mode="RGBA")
    elif norm.ndim == 3 and norm.shape[-1] == 3:
        img = Image.fromarray(norm, mode="RGB")
    else:
        img = Image.fromarray(norm, mode="L")

    w, h = img.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), img.size[0], img.size[1]


def encode_png_b64(png_bytes: bytes) -> str:
    return base64.b64encode(png_bytes).decode()


def thumbnail_field(array_2d: Any, max_dim: int = _MAX_THUMBNAIL_DIM) -> Dict[str, Any]:
    png, w, h = render_thumbnail(array_2d, max_dim=max_dim)
    return {
        "thumbnail_png_base64": encode_png_b64(png),
        "thumbnail_content_type": "image/png",
        "thumbnail_width": w,
        "thumbnail_height": h,
    }


def error(code: str, message: str) -> Dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def missing_dep(package: str, exc: Optional[BaseException] = None) -> Dict[str, Any]:
    detail = f": {exc}" if exc else ""
    return error(
        "parse_failed",
        f"Required library '{package}' is not available{detail}. "
        f"Install it via 'pip install -r backend/requirements.txt'.",
    )


__all__ = [
    "basic_stats",
    "encode_png_b64",
    "error",
    "middle_slice",
    "missing_dep",
    "render_thumbnail",
    "thumbnail_field",
]
