"""``read_image`` tool: deliver a normalized image to the connected vision model."""

from __future__ import annotations

import base64
import io
import logging
from typing import Any, Dict, Optional

from agents.general.file_tools import read_attachment_bytes

logger = logging.getLogger("FileTools.read_image")

_MAX_DIMENSION = 2048


def read_image(
    attachment_id: str,
    user_id: Optional[str] = None,
    **_ignored: Any,
) -> Dict[str, Any]:
    """Decode, resize, and base64-encode an image for the vision model."""
    att, payload, err = read_attachment_bytes(attachment_id, user_id)
    if err is not None:
        return err

    try:
        from PIL import Image  # type: ignore
    except Exception as exc:  # pragma: no cover
        return {"error": {"code": "parse_failed", "message": f"Pillow unavailable: {exc}"}}

    try:
        with Image.open(io.BytesIO(payload)) as img:
            img.load()
            original_format = img.format or att.extension.upper()
            w, h = img.size
            if max(w, h) > _MAX_DIMENSION:
                scale = _MAX_DIMENSION / max(w, h)
                img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
                w, h = img.size

            # Choose canonical encoding: PNG for lossless / images with alpha,
            # JPEG for everything else when the original is large.
            has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
            if has_alpha or att.size_bytes <= 1_048_576 or original_format == "PNG":
                fmt, content_type = "PNG", "image/png"
                if img.mode not in ("RGBA", "RGB"):
                    img = img.convert("RGBA" if has_alpha else "RGB")
            else:
                fmt, content_type = "JPEG", "image/jpeg"
                if img.mode != "RGB":
                    img = img.convert("RGB")

            buf = io.BytesIO()
            save_kwargs = {"format": fmt}
            if fmt == "JPEG":
                save_kwargs["quality"] = 90
            img.save(buf, **save_kwargs)
            data = base64.b64encode(buf.getvalue()).decode()
    except Exception as exc:
        logger.exception("image normalize failed")
        return {"error": {"code": "parse_failed", "message": str(exc)}}

    return {
        "filename": att.filename,
        "content_type": content_type,
        "width": w,
        "height": h,
        "image_base64": data,
    }


__all__ = ["read_image"]
