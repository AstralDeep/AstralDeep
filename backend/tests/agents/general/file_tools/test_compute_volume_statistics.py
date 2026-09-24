"""Tests for agents/general/file_tools/medical/compute_volume_statistics.py: histogram
bin-edge counts and MIP projections, including the minimum-bins clamp.
"""

from __future__ import annotations

import pytest

pytest.importorskip("numpy")
pytest.importorskip("nibabel")

from agents.general.file_tools.medical.compute_volume_statistics import (  # noqa: E402
    compute_volume_statistics,
)
from conftest import _persist, make_nifti  # noqa: E402


def test_histogram_and_projections(repo, upload_root):
    aid = _persist(
        repo, user_id="alice", filename="brain.nii",
        category="medical", extension="nii",
        content_type="application/octet-stream", upload_root=upload_root,
        payload=make_nifti((8, 8, 8)),
    )
    out = compute_volume_statistics(attachment_id=aid, user_id="alice", bins=16)
    assert "error" not in out
    hist = out["histogram"]
    assert len(hist["bin_edges"]) == 17
    assert len(hist["counts"]) == 16
    assert sum(hist["counts"]) > 0
    assert "mip_axial" in out and "mip_coronal" in out and "mip_sagittal" in out
    assert out["pixel_stats"]["shape"] == [8, 8, 8]


def test_bin_clamp(repo, upload_root):
    aid = _persist(
        repo, user_id="alice", filename="brain.nii",
        category="medical", extension="nii",
        content_type="application/octet-stream", upload_root=upload_root,
        payload=make_nifti((4, 4, 4)),
    )
    out = compute_volume_statistics(attachment_id=aid, user_id="alice", bins=1)
    assert len(out["histogram"]["counts"]) >= 2
