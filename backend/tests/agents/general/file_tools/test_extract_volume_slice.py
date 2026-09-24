"""Tests for agents/general/file_tools/medical/extract_volume_slice.py: default
middle-slice selection, explicit index, out-of-range handling, across NIfTI and MHA
volumes.
"""

from __future__ import annotations

import base64
import io

import pytest

pytest.importorskip("numpy")

from agents.general.file_tools.medical.extract_volume_slice import extract_volume_slice  # noqa: E402
from conftest import _persist, make_mha, make_nifti  # noqa: E402


def test_slice_nifti_default_middle(repo, upload_root):
    pytest.importorskip("nibabel")
    from PIL import Image  # type: ignore

    aid = _persist(
        repo, user_id="alice", filename="brain.nii",
        category="medical", extension="nii",
        content_type="application/octet-stream", upload_root=upload_root,
        payload=make_nifti((10, 12, 14)),
    )
    out = extract_volume_slice(attachment_id=aid, user_id="alice")
    assert "error" not in out
    assert out["axis_used"] == 2
    assert out["index_used"] == 7
    img = Image.open(io.BytesIO(base64.b64decode(out["thumbnail_png_base64"])))
    assert img.size[0] > 0 and img.size[1] > 0


def test_slice_explicit_index(repo, upload_root):
    pytest.importorskip("nibabel")

    aid = _persist(
        repo, user_id="alice", filename="brain.nii",
        category="medical", extension="nii",
        content_type="application/octet-stream", upload_root=upload_root,
        payload=make_nifti((6, 6, 6)),
    )
    out = extract_volume_slice(attachment_id=aid, user_id="alice", axis="x", index=1)
    assert "error" not in out
    assert out["axis_used"] == 0
    assert out["index_used"] == 1


def test_slice_out_of_range(repo, upload_root):
    pytest.importorskip("nibabel")

    aid = _persist(
        repo, user_id="alice", filename="brain.nii",
        category="medical", extension="nii",
        content_type="application/octet-stream", upload_root=upload_root,
        payload=make_nifti((6, 6, 6)),
    )
    out = extract_volume_slice(attachment_id=aid, user_id="alice", axis="z", index=999)
    assert "error" in out
    assert out["error"]["code"] == "parse_failed"


def test_slice_mha(repo, upload_root):
    pytest.importorskip("SimpleITK")

    aid = _persist(
        repo, user_id="alice", filename="v.mha",
        category="medical", extension="mha",
        content_type="application/octet-stream", upload_root=upload_root,
        payload=make_mha((4, 6, 8)),
    )
    out = extract_volume_slice(attachment_id=aid, user_id="alice", axis="y", index=2)
    assert "error" not in out
    assert out["axis_used"] == 1
    assert out["index_used"] == 2
