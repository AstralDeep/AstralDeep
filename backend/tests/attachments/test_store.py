"""Deep attachment identity behavior at Plane's streaming capability boundary."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from orchestrator.attachments.blob_access import (
    AttachmentBlobReferenceError,
    attachment_storage_key,
    blob_key_for_attachment,
    blob_key_from_storage_path,
    blob_store_from_orchestrator,
    metadata_storage_path,
    open_attachment_parser_lease,
    open_attachment_reader,
)


class _Reader:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def iter_chunks(self):
        yield self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None


class _RecordingBlobs:
    """Explicit fake for Deep's argument mapping, not Plane storage mechanics."""

    def __init__(self, payload: bytes = b"trusted parser input") -> None:
        self.payload = payload
        self.reader_calls: list[dict[str, object]] = []
        self.parser_calls: list[dict[str, object]] = []

    def open_reader(self, **values):
        self.reader_calls.append(dict(values))
        return _Reader(self.payload)

    @contextmanager
    def open_parser_lease(self, **values):
        self.parser_calls.append(dict(values))
        yield "scoped-parser-capability"


def _attachment(payload: bytes, **changes):
    values = {
        "attachment_id": "aid-1",
        "filename": "data.bin",
        "storage_path": "user-A/aid-1/data.bin",
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_deep_metadata_keys_are_canonical_and_owner_fenced():
    assert attachment_storage_key("aid-1", "data.bin") == "aid-1/data.bin"
    assert metadata_storage_path("user-A", "aid-1/data.bin") == (
        "user-A/aid-1/data.bin"
    )
    assert blob_key_from_storage_path(
        "user-A", "user-A/aid-1/data.bin"
    ) == "aid-1/data.bin"
    assert blob_key_from_storage_path(
        "user-A", r"user-A\aid-1\data.bin"
    ) == "aid-1/data.bin"
    assert blob_key_from_storage_path(
        "user-A", r"user-A\aid-1/data.bin"
    ) == "aid-1/data.bin"
    assert blob_key_for_attachment(_attachment(b"data"), "user-A") == (
        "aid-1/data.bin"
    )


@pytest.mark.parametrize(
    ("function", "arguments"),
    (
        (attachment_storage_key, ("", "data.bin")),
        (attachment_storage_key, ("aid-1", "")),
        (metadata_storage_path, ("", "aid-1/data.bin")),
        (metadata_storage_path, ("user-A", "../escape.bin")),
        (metadata_storage_path, ("user-A", "/absolute.bin")),
        (metadata_storage_path, ("user-A", "C:/escape.bin")),
        (metadata_storage_path, ("user-A", "aid-1//data.bin")),
        (blob_key_from_storage_path, ("", "user-A/aid-1/data.bin")),
        (blob_key_from_storage_path, ("user-A", "user-B/aid-1/data.bin")),
        (blob_key_from_storage_path, ("user-A", "user-A/only-two")),
    ),
)
def test_deep_metadata_key_rejections(function, arguments):
    with pytest.raises(AttachmentBlobReferenceError):
        function(*arguments)


def test_metadata_locator_drift_and_invalid_reader_metadata_fail_closed():
    blobs = _RecordingBlobs()
    with pytest.raises(AttachmentBlobReferenceError, match="disagrees"):
        blob_key_for_attachment(
            _attachment(b"data", storage_path="user-A/other/data.bin"),
            "user-A",
        )
    for invalid_size in (True, "not-a-size", -1):
        with pytest.raises(AttachmentBlobReferenceError, match="size_bytes"):
            open_attachment_reader(
                blobs,
                _attachment(b"data", size_bytes=invalid_size),
                "user-A",
            )
    with pytest.raises(AttachmentBlobReferenceError, match="SHA-256"):
        open_attachment_reader(
            blobs,
            _attachment(b"data", sha256=""),
            "user-A",
        )
    assert blobs.reader_calls == []


def test_reader_and_parser_lease_receive_exact_typed_identity_fences():
    payload = b"trusted parser input"
    attachment = _attachment(payload)
    blobs = _RecordingBlobs(payload)
    expected = {
        "owner_id": "user-A",
        "key": "aid-1/data.bin",
        "max_bytes": len(payload),
        "expected_size_bytes": len(payload),
        "expected_sha256": hashlib.sha256(payload).hexdigest(),
    }

    with open_attachment_reader(blobs, attachment, "user-A") as reader:
        assert b"".join(reader.iter_chunks()) == payload
    with open_attachment_parser_lease(blobs, attachment, "user-A") as capability:
        assert capability == "scoped-parser-capability"

    assert blobs.reader_calls == [expected]
    assert blobs.parser_calls == [expected]


def test_blob_store_resolution_prefers_injection_then_application_composition():
    blobs = _RecordingBlobs()
    assert blob_store_from_orchestrator(
        SimpleNamespace(attachment_blob_store=blobs)
    ) is blobs
    assert blob_store_from_orchestrator(
        SimpleNamespace(
            runtime_composition=SimpleNamespace(
                plane=SimpleNamespace(blobs=blobs)
            )
        )
    ) is blobs
    with pytest.raises(RuntimeError, match="not initialized"):
        blob_store_from_orchestrator(SimpleNamespace())
