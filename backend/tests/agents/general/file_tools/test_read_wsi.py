"""Tests for agents/general/file_tools/medical/read_wsi.py and extract_wsi_region.py:
pyramidal-TIFF level reporting, region extraction to PNG, and oversize-region
capping.
"""

from __future__ import annotations

import base64
import io

import pytest

pytest.importorskip("openslide")
pytest.importorskip("tifffile")

from agents.general.file_tools.medical.extract_wsi_region import extract_wsi_region  # noqa: E402
from agents.general.file_tools.medical.read_wsi import read_wsi  # noqa: E402
from conftest import _persist, make_pyramidal_tiff  # noqa: E402


def _persist_wsi(repo, upload_root):
    return _persist(
        repo, user_id="alice", filename="slide.tiff",
        category="medical", extension="tiff",
        content_type="image/tiff", upload_root=upload_root,
        payload=make_pyramidal_tiff(size=512, levels=3),
    )


def test_read_wsi_reports_levels(repo, upload_root):
    aid = _persist_wsi(repo, upload_root)
    out = read_wsi(attachment_id=aid, user_id="alice")
    assert "error" not in out
    assert out["level_count"] >= 1
    assert out["level_dimensions"]
    assert "thumbnail_png_base64" in out or "thumbnail_error" in out


def test_extract_wsi_region_returns_png(repo, upload_root):
    from PIL import Image  # type: ignore

    aid = _persist_wsi(repo, upload_root)
    out = extract_wsi_region(
        attachment_id=aid, user_id="alice",
        level=0, x=0, y=0, width=64, height=64,
    )
    assert "error" not in out
    assert out["content_type"] == "image/png"
    img = Image.open(io.BytesIO(base64.b64decode(out["image_base64"])))
    assert img.size == (64, 64)


def test_extract_wsi_region_caps_oversize(repo, upload_root):
    aid = _persist_wsi(repo, upload_root)
    out = extract_wsi_region(
        attachment_id=aid, user_id="alice",
        level=0, x=0, y=0, width=9999, height=9999,
    )
    assert "error" not in out
    assert out["width"] <= 2048 and out["height"] <= 2048
