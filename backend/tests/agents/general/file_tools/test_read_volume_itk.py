"""Tests for agents/general/file_tools/medical/read_volume_itk.py: NRRD and MHA volume
reading via SimpleITK.
"""

from __future__ import annotations

import pytest

pytest.importorskip("SimpleITK")
pytest.importorskip("numpy")

from agents.general.file_tools.medical.read_volume_itk import read_volume_itk  # noqa: E402
from conftest import _persist, make_mha, make_nrrd  # noqa: E402


def test_read_nrrd_volume(repo, upload_root):
    aid = _persist(
        repo, user_id="alice", filename="vol.nrrd",
        category="medical", extension="nrrd",
        content_type="application/octet-stream", upload_root=upload_root,
        payload=make_nrrd((8, 10, 12)),
    )
    out = read_volume_itk(attachment_id=aid, user_id="alice")
    assert "error" not in out
    assert out["size"] == [12, 10, 8]
    assert len(out["spacing"]) == 3
    assert "thumbnail_png_base64" in out


def test_read_mha_volume(repo, upload_root):
    aid = _persist(
        repo, user_id="alice", filename="vol.mha",
        category="medical", extension="mha",
        content_type="application/octet-stream", upload_root=upload_root,
        payload=make_mha((4, 6, 8)),
    )
    out = read_volume_itk(attachment_id=aid, user_id="alice")
    assert "error" not in out
    assert out["pixel_stats"]["shape"] == [4, 6, 8]
