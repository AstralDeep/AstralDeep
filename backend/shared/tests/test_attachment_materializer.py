"""Inline attachment policy over the application materialization service."""

from __future__ import annotations

import csv
import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import shared.attachment_materializer as materializer
from shared.attachment_materializer import (
    MAX_INLINE_BYTES,
    materialize_text_attachment,
    strip_code_fences,
)

CSV_TEXT = "Week,Enrollment\n1,40\n2,42\n3,45\n"


@pytest.fixture(autouse=True)
def materialization_binding(monkeypatch):
    service = MagicMock()

    def _materialize(**values):
        payload = b"".join(values["chunks"])
        return SimpleNamespace(
            attachment_id=values["attachment_id"],
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )

    service.materialize_bytes.side_effect = _materialize
    monkeypatch.setattr(materializer, "_MATERIALIZATION_SERVICE", service)
    return service


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (f"```csv\n{CSV_TEXT}```", CSV_TEXT.strip()),
        (f"```\n{CSV_TEXT}\n```", CSV_TEXT.strip()),
        (f"```text\n{CSV_TEXT}\n```", CSV_TEXT.strip()),
        (f"  {CSV_TEXT}  ", CSV_TEXT.strip()),
        ("```csv", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_strip_code_fences(source, expected) -> None:
    assert strip_code_fences(source) == expected


def test_validation_failures_happen_before_publication() -> None:
    with pytest.raises(ValueError, match="user_id is required"):
        materialize_text_attachment(CSV_TEXT, "")
    with pytest.raises(ValueError, match="empty"):
        materialize_text_attachment("```\n```", "alice")
    with pytest.raises(ValueError, match="no data rows"):
        materialize_text_attachment("Week,Enrollment\n", "alice")
    huge_field = "x" * (csv.field_size_limit() + 1)
    with pytest.raises(ValueError, match="not valid CSV"):
        materialize_text_attachment(f"Week,Notes\n1,{huge_field}\n", "alice")
    big = "a,b\n" + ("1,2\n" * ((MAX_INLINE_BYTES // 4) + 1))
    with pytest.raises(ValueError, match="inline limit"):
        materialize_text_attachment(big, "alice")
    with pytest.raises(ValueError, match="Unsupported"):
        materialize_text_attachment("anything", "alice", extension="exe")


def test_attachments_subsystem_unavailable_surfaces_value_error() -> None:
    import sys

    with patch.dict(sys.modules, {"orchestrator.attachments": None}):
        with pytest.raises(ValueError, match="Attachments subsystem unavailable"):
            materialize_text_attachment(CSV_TEXT, "alice")


def test_missing_service_binding_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(materializer, "_MATERIALIZATION_SERVICE", None)
    with pytest.raises(ValueError, match="no durable publisher"):
        materialize_text_attachment(CSV_TEXT, "alice")


def test_register_service_is_idempotent_and_rejects_rebinding(monkeypatch) -> None:
    first = MagicMock(materialize_bytes=MagicMock())
    second = MagicMock(materialize_bytes=MagicMock())
    monkeypatch.setattr(materializer, "_MATERIALIZATION_SERVICE", None)
    materializer.register_materialization_service(first)
    materializer.register_materialization_service(first)
    with pytest.raises(RuntimeError, match="already bound"):
        materializer.register_materialization_service(second)
    with pytest.raises(ValueError, match="service is required"):
        materializer.register_materialization_service(object())


def test_csv_publication_uses_central_durable_service(materialization_binding) -> None:
    attachment_id = materialize_text_attachment(f"```csv\n{CSV_TEXT}```", "alice")

    values = materialization_binding.materialize_bytes.call_args.kwargs
    assert values["attachment_id"] == attachment_id
    assert values["owner_id"] == "alice"
    assert values["extension"] == "csv"
    assert values["category"] == "spreadsheet"
    assert b"".join(values["chunks"]) == CSV_TEXT.strip().encode()
    assert values["resolve_content_type"](b"irrelevant") == "text/csv"


def test_non_csv_publication_skips_csv_validation(materialization_binding) -> None:
    attachment_id = materialize_text_attachment(
        "just some prose",
        "alice",
        extension="txt",
    )
    values = materialization_binding.materialize_bytes.call_args.kwargs
    assert values["attachment_id"] == attachment_id
    assert values["category"] == "text"
    assert values["resolve_content_type"](b"irrelevant") == "text/plain"


def test_service_failure_surfaces_without_direct_blob_cleanup(materialization_binding) -> None:
    materialization_binding.materialize_bytes.side_effect = RuntimeError("publication failed")
    with pytest.raises(ValueError, match="Could not record"):
        materialize_text_attachment(CSV_TEXT, "alice")
