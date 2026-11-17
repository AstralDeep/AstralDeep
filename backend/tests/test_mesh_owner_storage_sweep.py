"""Feeds the stored mesh owner id (mesh.<hex>) to every place an owner id becomes a file path, a URL segment or a SQL LIKE pattern, and shows the kernel's sha256: wire form is what those places refuse.
The sites are orchestrator/auth.py and the agent tools' tmp joins, attachments/blob_access.py with Plane's blob store, Plane's harness cleanup, and the framework credential owner segment."""

from __future__ import annotations

import importlib
import os
import shutil
import uuid
from pathlib import Path

import pytest
from astralplane import create_streaming_blob_store
from astralplane.blob_store import validate_blob_owner_id
from fastapi.responses import FileResponse

from orchestrator.attachments import blob_access
from shared import node_identity
from tests.helpers.attachment_materialization import publish_attachment_for_test
from tests.helpers.voice_plane_runtime import history_manager, isolated_plane_runtime

MESH = "sha256:" + "5" * 64
STORED = node_identity.owner_id_for_mesh(MESH)
NEIGHBOUR = node_identity.owner_id_for_mesh("sha256:" + "5" * 63 + "6")
BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def plane():
    with isolated_plane_runtime("mesh_owner_sweep") as runtime:
        yield runtime


@pytest.fixture
def owner_tmp():
    session = f"sweep-{uuid.uuid4().hex}"
    roots = [BACKEND / "tmp" / owner for owner in (STORED, NEIGHBOUR)]
    created = [root for root in roots if not root.exists()]
    try:
        yield session
    finally:
        for root in roots:
            target = root / session
            if target.exists():
                shutil.rmtree(target)
        for root in created:
            if root.exists() and not any(root.iterdir()):
                root.rmdir()


def test_the_stored_owner_is_one_safe_path_segment_and_the_wire_form_is_not():
    assert "/" not in STORED and os.sep not in STORED and ":" not in STORED and len(STORED) == 69
    assert validate_blob_owner_id(STORED) == STORED
    with pytest.raises(Exception):
        validate_blob_owner_id(MESH)


async def test_the_download_route_serves_only_the_mesh_owners_tmp_directory(owner_tmp):
    from orchestrator.auth import download_file

    mine = BACKEND / "tmp" / STORED / owner_tmp
    mine.mkdir(parents=True)
    (BACKEND / "tmp" / NEIGHBOUR / owner_tmp).mkdir(parents=True)
    (mine / "report.csv").write_bytes(b"a,b\n")
    served = await download_file(owner_tmp, "report.csv", user_id=STORED)
    assert isinstance(served, FileResponse) and Path(served.path) == mine / "report.csv"
    assert (await download_file(owner_tmp, "report.csv", user_id=NEIGHBOUR)).status_code == 404
    assert (await download_file(owner_tmp, "../../" + STORED + "/" + owner_tmp + "/report.csv",
                                user_id=NEIGHBOUR)).status_code == 403


def test_agent_tools_write_and_confine_files_under_the_mesh_owner(owner_tmp):
    from agents.connectors.mcp_tools_office import _write_download_file
    from agents.general.mcp_tools import _confine_input_path

    url = _write_download_file({"user_id": STORED, "session_id": owner_tmp}, "sheet.csv", b"x")
    written = BACKEND / "tmp" / STORED / owner_tmp / "sheet.csv"
    assert url == f"/api/download/{owner_tmp}/sheet.csv" and written.read_bytes() == b"x"
    assert _confine_input_path(str(written), STORED) == os.path.realpath(written)
    assert _confine_input_path(str(written), NEIGHBOUR) is None
    assert _confine_input_path(str(written), STORED[:-1]) is None


def test_attachment_paths_round_trip_under_the_mesh_owner():
    path = blob_access.metadata_storage_path(STORED, "att-1/file.txt")
    assert path == f"{STORED}/att-1/file.txt"
    assert blob_access.blob_key_from_storage_path(STORED, path) == "att-1/file.txt"
    with pytest.raises(blob_access.AttachmentBlobReferenceError):
        blob_access.blob_key_from_storage_path(NEIGHBOUR, path)


def test_an_attachment_for_the_mesh_owner_is_stored_and_read_back_through_plane(plane, tmp_path):
    blobs = create_streaming_blob_store(root=tmp_path / "attachments")
    attachment_id = str(uuid.uuid4())
    try:
        ready = publish_attachment_for_test(
            plane, plane.repositories, blobs, owner_id=STORED, attachment_id=attachment_id,
            filename="note.md", content_type="text/markdown", category="text", extension="md",
            chunks=(b"kept",), max_bytes=4,
        )
        with blob_access.open_attachment_reader(blobs, ready, STORED) as reader:
            assert reader.read() == b"kept"
        assert not blobs.is_owner_absent(owner_id=STORED)
        assert blobs.is_owner_absent(owner_id=NEIGHBOUR)
        with pytest.raises(Exception):
            blobs.is_owner_absent(owner_id=MESH)
    finally:
        plane.execute("DELETE FROM user_attachments WHERE user_id = ?", (STORED,))
        blobs.close()


@pytest.mark.parametrize("module", ["verification.isolation", "security_benchmark.isolation"])
@pytest.mark.parametrize("run_id", ["mesh", "mesh.", STORED, "run-1"])
def test_harness_cleanup_like_prefixes_never_match_a_mesh_owners_rows(plane, module, run_id):
    history = history_manager(plane)
    harness = importlib.import_module(module)
    chat = history.create_chat(user_id=STORED)
    history.add_message(chat, "user", "survives cleanup", STORED)
    doomed = harness.principal_id(run_id, "sweep")
    swept = history.create_chat(user_id=doomed)
    history.add_message(swept, "user", "removed by cleanup", doomed)
    try:
        assert harness.teardown(plane_runtime=plane, plane_repositories=plane.repositories, run_id=run_id) >= 1
        assert history.get_chat(swept, doomed) is None
        assert [m["content"] for m in history.get_chat(chat, STORED)["messages"]] == ["survives cleanup"]
    finally:
        history.delete_chat(chat, STORED)


def test_the_framework_credential_owner_segment_round_trips_a_mesh_owner():
    from orchestrator import framework_credentials

    segment = framework_credentials._encode_owner(STORED)
    assert "." not in segment and framework_credentials._decode_owner(segment) == STORED
