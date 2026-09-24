"""Tests for agents/general/file_tools/medical/read_nifti.py: orthogonal thumbnail
generation and gzipped-NIfTI handling.
"""

from __future__ import annotations

import pytest

pytest.importorskip("nibabel")
pytest.importorskip("numpy")

from agents.general.file_tools.medical.read_nifti import read_nifti  # noqa: E402
from conftest import _persist, make_nifti  # noqa: E402


def test_read_nifti_returns_orthogonal_thumbnails(repo, upload_root):
    aid = _persist(
        repo, user_id="alice", filename="brain.nii",
        category="medical", extension="nii",
        content_type="application/octet-stream", upload_root=upload_root,
        payload=make_nifti((10, 12, 14)),
    )
    out = read_nifti(attachment_id=aid, user_id="alice")
    assert "error" not in out
    assert out["shape"] == [10, 12, 14]
    assert len(out["voxel_sizes"]) == 3
    assert "axial" in out and "thumbnail_png_base64" in out["axial"]
    assert "coronal" in out and "thumbnail_png_base64" in out["coronal"]
    assert "sagittal" in out and "thumbnail_png_base64" in out["sagittal"]
    assert out["pixel_stats"]["shape"] == [10, 12, 14]


def test_read_nifti_gzipped(repo, upload_root):
    aid = _persist(
        repo, user_id="alice", filename="brain.nii.gz",
        category="medical", extension="nii.gz",
        content_type="application/gzip", upload_root=upload_root,
        payload=make_nifti((6, 6, 6), gz=True),
    )
    out = read_nifti(attachment_id=aid, user_id="alice")
    assert "error" not in out
    assert out["shape"] == [6, 6, 6]
