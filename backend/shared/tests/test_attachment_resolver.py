"""Tests for owner-scoped attachment readers and parser leases."""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from astralplane.errors import PlaneError
from orchestrator.attachments.blob_access import AttachmentBlobReferenceError

import shared.attachment_resolver as resolver
from shared.attachment_resolver import (
    open_attachment_blob_reader,
    open_attachment_parser_lease,
)


class _FixtureReader:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def iter_chunks(self):
        yield self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None


class _FixtureParserCapability(os.PathLike[str]):
    def __init__(self, path: Path) -> None:
        self._path = path
        self._active = True

    def __fspath__(self) -> str:
        if not self._active:
            raise PlaneError("parser lease is closed", code="blob_lease_closed")
        return os.fspath(self._path)

    def revoke(self) -> None:
        self._active = False


class _FixtureBlobStore:
    """Reader-only test double; fixture publication never bypasses Plane storage."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._payloads: dict[tuple[str, str], bytes] = {}

    def publish_fixture(self, *, owner_id: str, key: str, payload: bytes) -> None:
        self._payloads[(owner_id, key)] = payload

    def is_owner_absent(self, *, owner_id: str) -> bool:
        return not any(owner == owner_id for owner, _key in self._payloads)

    def _payload(self, values: dict[str, object]) -> bytes:
        identity = (str(values["owner_id"]), str(values["key"]))
        try:
            payload = self._payloads[identity]
        except KeyError as exc:
            raise PlaneError("blob not found", code="blob_not_found") from exc
        if len(payload) != values["expected_size_bytes"]:
            raise PlaneError("blob size mismatch", code="blob_integrity_mismatch")
        if hashlib.sha256(payload).hexdigest() != values["expected_sha256"]:
            raise PlaneError("blob digest mismatch", code="blob_integrity_mismatch")
        return payload

    def open_reader(self, **values):
        return _FixtureReader(self._payload(values))

    @contextmanager
    def open_parser_lease(self, **values):
        payload = self._payload(values)
        path = self._root / "parser-input"
        path.write_bytes(payload)
        capability = _FixtureParserCapability(path)
        try:
            yield capability
        finally:
            capability.revoke()


@pytest.fixture
def plane_binding(tmp_path, monkeypatch):
    runtime = MagicMock()
    repositories = MagicMock()
    blobs = _FixtureBlobStore(tmp_path)
    monkeypatch.setattr(resolver, "_PLANE_RUNTIME", runtime)
    monkeypatch.setattr(resolver, "_PLANE_REPOSITORIES", repositories)
    monkeypatch.setattr(resolver, "_PLANE_BLOBS", blobs)
    return runtime, repositories, blobs


def _record(*, payload: bytes = b"a,b\n1,2\n") -> SimpleNamespace:
    return SimpleNamespace(
        attachment_id="att-real",
        filename="data.csv",
        storage_path="alice/att-real/data.csv",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _publish(blobs, *, payload: bytes = b"a,b\n1,2\n") -> None:
    blobs.publish_fixture(
        owner_id="alice",
        key="att-real/data.csv",
        payload=payload,
    )


def test_empty_handle_raises(plane_binding) -> None:
    with pytest.raises(ValueError, match="required"):
        with open_attachment_parser_lease("", "alice"):
            pass


@pytest.mark.parametrize("untrusted", ("/etc/passwd", "C:\\Windows\\win.ini"))
def test_paths_are_only_unknown_handles(plane_binding, untrusted: str) -> None:
    fake_repo = MagicMock()
    fake_repo.get_by_id.return_value = None
    with patch(
        "orchestrator.attachments.repository.AttachmentRepository",
        return_value=fake_repo,
    ):
        with pytest.raises(ValueError, match="not a valid attachment"):
            with open_attachment_parser_lease(untrusted, "alice"):
                pass
    fake_repo.get_by_id.assert_called_once_with(untrusted, "alice")


def test_user_isolation_enforced_before_blob_access(plane_binding) -> None:
    _runtime, _repositories, blobs = plane_binding
    fake_repo = MagicMock()
    fake_repo.get_by_id.return_value = None
    with patch(
        "orchestrator.attachments.repository.AttachmentRepository",
        return_value=fake_repo,
    ), pytest.raises(ValueError, match="not a valid attachment"):
        with open_attachment_blob_reader("att-belongs-to-bob", "alice"):
            pass
    assert blobs.is_owner_absent(owner_id="alice")


def test_missing_blob_is_an_actionable_value_error(plane_binding) -> None:
    fake_repo = MagicMock()
    fake_repo.get_by_id.return_value = _record()
    with patch(
        "orchestrator.attachments.repository.AttachmentRepository",
        return_value=fake_repo,
    ), pytest.raises(ValueError, match="unavailable"):
        with open_attachment_parser_lease("att-real", "alice"):
            pass


def test_reader_is_bounded_and_digest_fenced(plane_binding) -> None:
    _runtime, _repositories, blobs = plane_binding
    payload = b"a,b\n1,2\n"
    _publish(blobs, payload=payload)
    fake_repo = MagicMock()
    fake_repo.get_by_id.return_value = _record(payload=payload)
    with patch(
        "orchestrator.attachments.repository.AttachmentRepository",
        return_value=fake_repo,
    ), open_attachment_blob_reader("att-real", "alice") as (attachment, reader):
        assert attachment.attachment_id == "att-real"
        assert b"".join(reader.iter_chunks()) == payload


def test_parser_capability_is_revoked_at_scope_exit(plane_binding) -> None:
    _runtime, _repositories, blobs = plane_binding
    _publish(blobs)
    fake_repo = MagicMock()
    fake_repo.get_by_id.return_value = _record()
    with patch(
        "orchestrator.attachments.repository.AttachmentRepository",
        return_value=fake_repo,
    ):
        with open_attachment_parser_lease("att-real", "alice") as capability:
            with open(capability, encoding="utf-8") as source:
                assert source.readline().strip() == "a,b"
        with pytest.raises(PlaneError, match="lease"):
            os.fspath(capability)


def test_missing_plane_binding_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(resolver, "_PLANE_RUNTIME", None)
    monkeypatch.setattr(resolver, "_PLANE_REPOSITORIES", None)
    monkeypatch.setattr(resolver, "_PLANE_BLOBS", None)
    with pytest.raises(ValueError, match="no AstralPlane runtime"):
        with open_attachment_parser_lease("att-real", "alice"):
            pass


def test_register_plane_runtime_is_idempotent_but_rejects_rebinding(
    monkeypatch,
) -> None:
    runtime = MagicMock()
    repositories = MagicMock()
    blobs = MagicMock()
    monkeypatch.setattr(resolver, "_PLANE_RUNTIME", None)
    monkeypatch.setattr(resolver, "_PLANE_REPOSITORIES", None)
    monkeypatch.setattr(resolver, "_PLANE_BLOBS", None)

    resolver.register_plane_runtime(runtime, repositories, blobs)
    resolver.register_plane_runtime(runtime, repositories, blobs)
    with pytest.raises(RuntimeError, match="already bound"):
        resolver.register_plane_runtime(MagicMock(), repositories, blobs)


def test_register_plane_runtime_validates_every_dependency(monkeypatch) -> None:
    monkeypatch.setattr(resolver, "_PLANE_RUNTIME", None)
    monkeypatch.setattr(resolver, "_PLANE_REPOSITORIES", None)
    monkeypatch.setattr(resolver, "_PLANE_BLOBS", None)
    with pytest.raises(ValueError, match="runtime"):
        resolver.register_plane_runtime(None, object(), object())
    with pytest.raises(ValueError, match="catalog"):
        resolver.register_plane_runtime(object(), None, object())
    with pytest.raises(ValueError, match="blob store"):
        resolver.register_plane_runtime(MagicMock(repositories=object()), None, None)


@pytest.mark.parametrize("changed", ("repositories", "blobs"))
def test_register_plane_runtime_rejects_catalog_or_blob_rebinding(
    monkeypatch,
    changed,
) -> None:
    runtime, repositories, blobs = object(), object(), object()
    monkeypatch.setattr(resolver, "_PLANE_RUNTIME", runtime)
    monkeypatch.setattr(resolver, "_PLANE_REPOSITORIES", repositories)
    monkeypatch.setattr(resolver, "_PLANE_BLOBS", blobs)
    if changed == "repositories":
        with pytest.raises(RuntimeError, match="catalog"):
            resolver.register_plane_runtime(runtime, object(), blobs)
    else:
        with pytest.raises(RuntimeError, match="blob store"):
            resolver.register_plane_runtime(runtime, repositories, object())


def test_reader_integrity_failure_is_actionable(plane_binding, monkeypatch) -> None:
    fake_repo = MagicMock()
    fake_repo.get_by_id.return_value = _record()

    @contextmanager
    def _unavailable(*_args, **_kwargs):
        raise AttachmentBlobReferenceError("tampered")
        yield  # pragma: no cover

    monkeypatch.setattr(resolver, "_open_reader", _unavailable)
    with patch(
        "orchestrator.attachments.repository.AttachmentRepository",
        return_value=fake_repo,
    ), pytest.raises(ValueError, match="unavailable"):
        with open_attachment_blob_reader("att-real", "alice"):
            pass
