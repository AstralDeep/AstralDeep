"""Tests for orchestrator/attachments/content_type.py: extension-to-category/MIME
mapping, legacy binary-format flags, compound-suffix normalization, and per-category
size caps.
"""

from __future__ import annotations

import pytest

from orchestrator.attachments import content_type as ct


@pytest.mark.parametrize("name,ext,category", [
    ("Q4-report.PDF", "pdf", "document"),
    ("notes.docx", "docx", "document"),
    ("data.xlsx", "xlsx", "spreadsheet"),
    ("slides.pptx", "pptx", "presentation"),
    ("config.yaml", "yaml", "text"),
    ("script.py", "py", "text"),
    ("photo.JPEG", "jpeg", "image"),
])
def test_extension_and_category(name, ext, category):
    assert ct.normalise_extension(name) == ext
    assert ct.category_for_extension(ext) == category


def test_unknown_extension_returns_none():
    assert ct.category_for_extension("dwg") is None
    assert ct.category_for_extension("") is None


def test_legacy_binary_formats_are_marked():
    assert "doc" in ct.LEGACY_BINARY_FORMATS
    assert "ppt" in ct.LEGACY_BINARY_FORMATS


@pytest.mark.parametrize("ext,mime,ok", [
    ("pdf", "application/pdf", True),
    ("pdf", "application/octet-stream", False),
    ("docx", "application/zip", True),
    ("png", "image/png", True),
    ("png", "image/jpeg", False),
    ("py",  "text/x-python", True),
    ("csv", "text/csv", True),
    ("csv", "image/png", False),
])
def test_is_consistent(ext, mime, ok):
    assert ct.is_consistent(ext, mime) is ok


def test_is_consistent_unknown_extension_is_false():
    assert ct.is_consistent("dwg", "application/octet-stream") is False


@pytest.mark.parametrize("name,ext,category", [
    ("scan.dcm", "dcm", "medical"),
    ("scan.DICOM", "dicom", "medical"),
    ("volume.nii", "nii", "medical"),
    ("volume.nii.gz", "nii.gz", "medical"),
    ("slide.czi", "czi", "medical"),
    ("vol.nrrd", "nrrd", "medical"),
    ("vol.mha", "mha", "medical"),
    ("vol.mhd", "mhd", "medical"),
    ("stack.ome.tif", "ome.tif", "medical"),
    ("stack.ome.tiff", "ome.tiff", "medical"),
    ("photo.tiff", "tiff", "medical"),
    ("slide.svs", "svs", "medical"),
    ("slide.ndpi", "ndpi", "medical"),
])
def test_medical_extensions_are_accepted(name, ext, category):
    assert ct.normalise_extension(name) == ext
    assert ct.category_for_extension(ext) == category


def test_compound_extension_takes_precedence_over_last_dot():
    assert ct.normalise_extension("brain.nii.gz") == "nii.gz"
    assert ct.normalise_extension("x.ome.tif") == "ome.tif"
    assert ct.normalise_extension("x.ome.tiff") == "ome.tiff"


def test_max_bytes_for_category_caps():
    assert ct.max_bytes_for_category("document") == 30 * 1024 * 1024
    assert ct.max_bytes_for_category("image") == 30 * 1024 * 1024
    assert ct.max_bytes_for_category("medical") == 2 * 1024 * 1024 * 1024
    assert ct.max_bytes_for_category("__unknown__") == 30 * 1024 * 1024
