"""Tests for agents/general/file_tools/medical/read_bio_tiff.py: OME-XML detection and
plain-TIFF handling.
"""

from __future__ import annotations

import pytest

pytest.importorskip("tifffile")
pytest.importorskip("numpy")

from agents.general.file_tools.medical.read_bio_tiff import read_bio_tiff  # noqa: E402
from conftest import _persist, make_ome_tiff, make_tiff  # noqa: E402


def test_read_ome_tiff_detects_ome_xml(repo, upload_root):
    aid = _persist(
        repo, user_id="alice", filename="stack.ome.tif",
        category="medical", extension="ome.tif",
        content_type="image/tiff", upload_root=upload_root,
        payload=make_ome_tiff((3, 16, 16)),
    )
    out = read_bio_tiff(attachment_id=aid, user_id="alice")
    assert "error" not in out
    assert out["is_ome"] is True
    assert out["series"]
    assert "thumbnail_png_base64" in out


def test_read_plain_tiff(repo, upload_root):
    aid = _persist(
        repo, user_id="alice", filename="photo.tif",
        category="medical", extension="tif",
        content_type="image/tiff", upload_root=upload_root,
        payload=make_tiff(40, 30),
    )
    out = read_bio_tiff(attachment_id=aid, user_id="alice")
    assert "error" not in out
    assert out["series"]
    assert "thumbnail_png_base64" in out
