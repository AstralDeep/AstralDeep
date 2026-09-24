"""Tests for agents/general/file_tools/medical/read_dicom.py: default PHI stripping,
opt-in PHI fields, thumbnail generation, and not-found handling.
"""

from __future__ import annotations

import base64
import io

import pytest

pytest.importorskip("pydicom")
pytest.importorskip("numpy")

from agents.general.file_tools.medical.read_dicom import read_dicom  # noqa: E402
from conftest import _persist, make_dicom  # noqa: E402


def test_read_dicom_strips_phi_by_default(repo, upload_root):
    aid = _persist(
        repo, user_id="alice", filename="scan.dcm",
        category="medical", extension="dcm",
        content_type="application/dicom", upload_root=upload_root,
        payload=make_dicom(32, 32, patient_name="DOE^JOHN", patient_id="MRN-999"),
    )
    out = read_dicom(attachment_id=aid, user_id="alice")
    assert "error" not in out
    assert out["phi_included"] is False
    assert "phi" not in out
    meta = out["metadata"]
    assert meta["Modality"] == "CT"
    assert meta["Rows"] == 32 and meta["Columns"] == 32
    # PHI must never leak into the default metadata block
    for banned in ("PatientName", "PatientID", "PatientBirthDate",
                    "ReferringPhysicianName", "InstitutionName",
                    "AccessionNumber", "StudyDate"):
        assert banned not in meta


def test_read_dicom_include_phi_surfaces_patient_fields(repo, upload_root):
    aid = _persist(
        repo, user_id="alice", filename="scan.dcm",
        category="medical", extension="dcm",
        content_type="application/dicom", upload_root=upload_root,
        payload=make_dicom(patient_name="DOE^JANE", patient_id="MRN-42"),
    )
    out = read_dicom(attachment_id=aid, user_id="alice", include_phi=True)
    assert "error" not in out
    assert out["phi_included"] is True
    phi = out["phi"]
    assert "DOE" in str(phi["PatientName"])
    assert phi["PatientID"] == "MRN-42"


def test_read_dicom_returns_thumbnail(repo, upload_root):
    pytest.importorskip("PIL")
    from PIL import Image  # type: ignore

    aid = _persist(
        repo, user_id="alice", filename="scan.dcm",
        category="medical", extension="dcm",
        content_type="application/dicom", upload_root=upload_root,
        payload=make_dicom(48, 48),
    )
    out = read_dicom(attachment_id=aid, user_id="alice")
    assert "thumbnail_png_base64" in out
    img = Image.open(io.BytesIO(base64.b64decode(out["thumbnail_png_base64"])))
    assert img.size == (48, 48)
    assert out["pixel_stats"]["shape"] == [48, 48]


def test_read_dicom_not_found(repo, upload_root):
    out = read_dicom(attachment_id="nope", user_id="alice")
    assert "error" in out
    assert out["error"]["code"] == "not_found"
