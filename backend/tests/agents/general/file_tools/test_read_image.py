"""Tests for agents/general/file_tools/read_image.py: base64 PNG envelope and
resize-to-2048 normalization.
"""

from __future__ import annotations

import base64
import io

from PIL import Image  # type: ignore

from agents.general.file_tools.read_image import read_image
from conftest import _persist, make_png


def test_read_png_returns_base64(repo, upload_root):
    aid = _persist(repo, user_id="alice", filename="x.png",
                   category="image", extension="png",
                   content_type="image/png", upload_root=upload_root,
                   payload=make_png(64, 64))
    out = read_image(attachment_id=aid, user_id="alice")
    assert "error" not in out
    assert out["content_type"] == "image/png"
    assert out["width"] == 64 and out["height"] == 64
    raw = base64.b64decode(out["image_base64"])
    img = Image.open(io.BytesIO(raw))
    assert img.size == (64, 64)


def test_read_image_resizes_to_2048(repo, upload_root):
    aid = _persist(repo, user_id="alice", filename="big.png",
                   category="image", extension="png",
                   content_type="image/png", upload_root=upload_root,
                   payload=make_png(4000, 2000))
    out = read_image(attachment_id=aid, user_id="alice")
    assert out["width"] <= 2048 and out["height"] <= 2048
    assert out["width"] == 2048
