"""Dispatcher: ownership enforcement and basic resolution."""

from __future__ import annotations

import inspect
import uuid
import os

import pytest
from astralplane.errors import PlaneError

import agents.general.file_tools as file_tools
from agents.general.file_tools import (
    attachment_parser_scope,
    read_attachment_bytes,
    resolve_attachment,
)
from conftest import _persist, make_png


def test_resolve_requires_user_id(repo, upload_root):
    aid = _persist(repo, user_id="alice", filename="x.png",
                   category="image", extension="png",
                   content_type="image/png", upload_root=upload_root,
                   payload=make_png())
    att, path, err = resolve_attachment(aid, user_id=None)
    assert att is None and path is None
    assert err["error"]["code"] == "not_found"


def test_resolve_rejects_foreign_user(repo, upload_root):
    aid = _persist(repo, user_id="alice", filename="x.png",
                   category="image", extension="png",
                   content_type="image/png", upload_root=upload_root,
                   payload=make_png())
    att, path, err = resolve_attachment(aid, user_id="bob")
    assert att is None and err["error"]["code"] == "not_found"


def test_resolve_unknown_id(repo, upload_root):
    att, path, err = resolve_attachment(str(uuid.uuid4()), user_id="alice")
    assert att is None and err["error"]["code"] == "not_found"


def test_resolve_requires_attachment_id():
    att, path, err = resolve_attachment("", user_id="alice")
    assert att is None and path is None
    assert err["error"]["code"] == "not_found"


def test_resolve_happy_path(repo, upload_root):
    aid = _persist(repo, user_id="alice", filename="x.png",
                   category="image", extension="png",
                   content_type="image/png", upload_root=upload_root,
                   payload=make_png())
    @attachment_parser_scope
    def resolve_in_parser_scope():
        att, path, err = resolve_attachment(aid, user_id="alice")
        assert err is None
        assert path is not None and os.path.exists(path)
        return att, path, err

    att, capability, err = resolve_in_parser_scope()
    assert err is None
    assert att.attachment_id == aid
    with pytest.raises(PlaneError, match="lease"):
        os.fspath(capability)


def test_path_parser_must_enter_scoped_lease(repo, upload_root):
    aid = _persist(
        repo,
        user_id="alice",
        filename="x.png",
        category="image",
        extension="png",
        content_type="image/png",
        upload_root=upload_root,
        payload=make_png(),
    )
    att, path, err = resolve_attachment(aid, user_id="alice")
    assert att is None and path is None
    assert err["error"]["code"] == "unreadable_file"


def test_path_parser_rejects_content_mismatch(repo, upload_root):
    aid = _persist(
        repo,
        user_id="alice",
        filename="x.png",
        category="image",
        extension="png",
        content_type="image/png",
        upload_root=upload_root,
        payload=b"plain text, not a png",
    )
    @attachment_parser_scope
    def resolve_in_parser_scope():
        return resolve_attachment(aid, user_id="alice")

    att, path, err = resolve_in_parser_scope()
    assert att is None and path is None
    assert err["error"]["code"] == "unreadable_file"


def test_path_parser_surfaces_blob_integrity_failure(repo, upload_root):
    payload = make_png()
    aid = _persist(
        repo,
        user_id="alice",
        filename="x.png",
        category="image",
        extension="png",
        content_type="image/png",
        upload_root=upload_root,
        payload=payload,
    )
    (upload_root / "alice" / aid / "x.png").write_bytes(b"x" * len(payload))
    @attachment_parser_scope
    def resolve_in_parser_scope():
        return resolve_attachment(aid, user_id="alice")

    att, path, err = resolve_in_parser_scope()
    assert att is None and path is None
    assert err["error"]["code"] == "not_found"


def test_byte_reader_negative_paths(repo, upload_root, monkeypatch):
    assert read_attachment_bytes("anything", None)[2]["error"]["code"] == "not_found"
    assert read_attachment_bytes("", "alice")[2]["error"]["code"] == "not_found"
    assert read_attachment_bytes("missing", "alice")[2]["error"]["code"] == "not_found"

    aid = _persist(
        repo,
        user_id="alice",
        filename="x.txt",
        category="text",
        extension="txt",
        content_type="text/plain",
        upload_root=upload_root,
        payload=b"hello",
    )
    monkeypatch.setattr(
        file_tools.ct,
        "sniff_content_type",
        lambda _payload: "image/png",
    )
    att, payload, err = read_attachment_bytes(aid, "alice")
    assert att is None and payload is None
    assert err["error"]["code"] == "unreadable_file"


def test_dependency_bindings_fail_closed_and_reject_rebinding(monkeypatch):
    monkeypatch.setattr(file_tools, "_TEST_DEPENDENCIES", None)
    monkeypatch.setattr(file_tools, "_PRODUCTION_DEPENDENCIES", None)
    assert resolve_attachment("anything", "alice")[2]["error"]["code"] == "not_found"
    assert read_attachment_bytes("anything", "alice")[2]["error"]["code"] == "not_found"
    with pytest.raises(ValueError, match="all Plane"):
        file_tools.set_plane_dependencies_for_testing(object(), None, object())
    with pytest.raises(ValueError, match="all Plane"):
        file_tools.register_plane_dependencies(None, object(), object())

    runtime, repositories, blobs = object(), object(), object()
    file_tools.register_plane_dependencies(runtime, repositories, blobs)
    file_tools.register_plane_dependencies(runtime, repositories, blobs)
    with pytest.raises(RuntimeError, match="already bound"):
        file_tools.register_plane_dependencies(object(), repositories, blobs)


def test_parser_lease_open_failure_is_opaque(repo, upload_root, monkeypatch):
    aid = _persist(
        repo,
        user_id="alice",
        filename="x.png",
        category="image",
        extension="png",
        content_type="image/png",
        upload_root=upload_root,
        payload=make_png(),
    )

    def _fail(*_args, **_kwargs):
        raise file_tools.AttachmentBlobReferenceError("unavailable")

    monkeypatch.setattr(file_tools, "open_attachment_parser_lease", _fail)
    @attachment_parser_scope
    def resolve_in_parser_scope():
        return resolve_attachment(aid, user_id="alice")

    att, path, err = resolve_in_parser_scope()
    assert att is None and path is None
    assert err["error"]["code"] == "not_found"


def test_byte_capable_readers_never_request_a_parser_path() -> None:
    from agents.general.file_tools import (
        ocr,
        read_document,
        read_image,
        read_presentation,
        read_spreadsheet,
    )
    for module in (
        read_document,
        read_image,
        read_presentation,
        read_spreadsheet,
    ):
        source = inspect.getsource(module)
        assert "read_attachment_bytes" in source
        assert "resolve_attachment" not in source
        assert "attachment_parser_scope" not in source

    rasterizer = inspect.getsource(ocr)
    assert "convert_from_bytes" in rasterizer
    assert "convert_from_path" not in rasterizer


def test_random_access_medical_readers_use_scoped_parser_leases() -> None:
    from agents.general.file_tools.medical import read_bio_tiff, read_czi, read_dicom

    for module in (read_bio_tiff, read_czi, read_dicom):
        source = inspect.getsource(module)
        assert "read_attachment_bytes" not in source
        assert "resolve_attachment" in source
        assert "attachment_parser_scope" in source
