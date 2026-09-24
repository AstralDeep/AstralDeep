"""Tests for orchestrator/attachments/content_type.py's broadened allow-list: new
text/data/archive categories, unchanged legacy mappings, auto-parse category
selection, and MIME-consistency coverage per extension.
"""

from __future__ import annotations

import pytest

from orchestrator.attachments import content_type as ct


@pytest.mark.parametrize("ext,expected_cat", [
    ("toml", "text"), ("ini", "text"), ("rst", "text"), ("tex", "text"),
    ("java", "text"), ("go", "text"), ("rs", "text"), ("rb", "text"),
    ("kt", "text"), ("swift", "text"), ("ipynb", "text"), ("jsonl", "text"),
    ("geojson", "text"), ("proto", "text"), ("graphql", "text"),
    ("parquet", "data"), ("avro", "data"), ("feather", "data"),
    ("h5", "data"), ("npy", "data"), ("sqlite", "data"), ("db", "data"),
    ("zip", "archive"), ("tar", "archive"), ("gz", "archive"),
    ("7z", "archive"), ("rar", "archive"), ("epub", "archive"),
])
def test_broadened_extensions_have_expected_category(ext, expected_cat):
    assert ct.category_for_extension(ext) == expected_cat


def test_legacy_feature002_types_unchanged():
    assert ct.category_for_extension("pdf") == "document"
    assert ct.category_for_extension("csv") == "spreadsheet"
    assert ct.category_for_extension("png") == "image"
    assert ct.category_for_extension("nii.gz") == "medical"


def test_new_categories_have_size_caps():
    assert ct.MAX_BYTES_BY_CATEGORY["data"] == 100 * 1024 * 1024
    assert ct.MAX_BYTES_BY_CATEGORY["archive"] == 100 * 1024 * 1024
    assert ct.max_bytes_for_category("data") == 100 * 1024 * 1024
    assert ct.max_bytes_for_category("archive") == 100 * 1024 * 1024


def test_auto_parse_categories_are_the_uncovered_ones():
    assert set(ct.AUTO_PARSE_CATEGORIES) == {"data", "archive"}


def test_unknown_extension_still_rejected():
    assert ct.category_for_extension("exe") is None
    assert ct.category_for_extension("dll") is None


@pytest.mark.parametrize("ext,mime", [
    ("toml", "text/plain"),
    ("java", "text/x-java"),
    ("ipynb", "application/json"),
    ("zip", "application/zip"),
    ("gz", "application/gzip"),
    ("parquet", "application/octet-stream"),
    ("sqlite", "application/x-sqlite3"),
    ("epub", "application/epub+zip"),
])
def test_consistency_gate_accepts_plausible_mimes_for_new_types(ext, mime):
    assert ct.is_consistent(ext, mime) is True


def test_every_accepted_extension_has_a_consistency_entry():
    missing = [e for e in ct.ACCEPTED_EXTENSIONS
               if e not in ct._EXTENSION_TO_MIME_PREFIXES]
    assert missing == [], f"extensions accepted but unmatchable by sniff gate: {missing}"
