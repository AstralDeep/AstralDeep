"""Proves snapshot links are off on a personal node whatever FF_ARTIFACT_SHARING says: the flag reads false, /share/{token} answers 404, the /api/share routes refuse through their dependency, the chrome reports share off, one boot warning is logged, and stored share rows keep their bytes while node status counts them.
It drives shared/feature_flags.py, orchestrator/api.py, chrome_availability.py, boot_posture.py and node_mesh.py against the real Plane-backed ShareGrantStore."""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import MagicMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from orchestrator import chrome_availability, node_mesh, boot_posture  # noqa: E402
from orchestrator.api import share_router  # noqa: E402
from orchestrator.artifact_share import ShareGrantStore, set_share_store  # noqa: E402
from personalization.phi_gate import PHIGate, set_phi_gate  # noqa: E402
from shared import node_identity as ni  # noqa: E402
from shared.feature_flags import FeatureFlags, flags  # noqa: E402
from tests.helpers.voice_plane_runtime import isolated_plane_runtime  # noqa: E402

ENV = "FF_ARTIFACT_SHARING"


class _CleanAnalyzer:
    def analyze(self, text, language, entities, score_threshold):
        return []


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("snapshot_links_personal") as runtime:
        yield runtime


@pytest.fixture(autouse=True)
def store(plane_runtime, server_identity):
    real = ShareGrantStore(plane_runtime=plane_runtime, plane_repositories=plane_runtime.repositories)
    set_share_store(real)
    set_phi_gate(PHIGate(analyzer=_CleanAnalyzer(), build_if_missing=False))
    yield real
    set_share_store(None)
    set_phi_gate(None)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(share_router)
    app.state.orchestrator = MagicMock()
    return TestClient(app)


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _rows(plane_runtime, owner):
    with plane_runtime.transaction() as transaction:
        rows = transaction.fetch_all("SELECT * FROM share_grant WHERE user_id = %s ORDER BY id", (owner,))
    return [_plain(row) for row in rows]


async def _mint(store, owner):
    minted = await store.mint(
        user_id=owner, chat_id="chat-legacy", scope="canvas",
        snapshot_html="<p>kept</p>", snapshot_json=[{"type": "card", "title": "kept"}],
    )
    return minted["share_url"]


@pytest.fixture
def owner(plane_runtime, server_identity, monkeypatch):
    monkeypatch.setenv(ENV, "true")
    monkeypatch.setitem(flags._flags, "artifact_sharing", True)
    value = server_identity.owner(f"pytest-snapshot-{uuid.uuid4().hex[:12]}")
    yield value
    with plane_runtime.transaction() as transaction:
        transaction.execute("DELETE FROM share_grant WHERE user_id = %s", (value,))


def test_the_flag_reads_false_on_a_personal_node_and_keeps_its_value_elsewhere(monkeypatch, server_identity):
    monkeypatch.setenv(ENV, "true")
    assert FeatureFlags().is_enabled("artifact_sharing") is True
    monkeypatch.setenv(ni.PROFILE_ENV, ni.PERSONAL)
    assert FeatureFlags().is_enabled("artifact_sharing") is False
    assert FeatureFlags().is_enabled("artifact_export") is True
    monkeypatch.setenv(ni.PROFILE_ENV, ni.INSTITUTION)
    assert FeatureFlags().is_enabled("artifact_sharing") is True


@pytest.mark.asyncio
async def test_a_stored_link_answers_404_on_a_personal_node_and_its_row_keeps_its_bytes(
    plane_runtime, store, client, owner, monkeypatch
):
    url = await _mint(store, owner)
    assert client.get(url).status_code == 200
    before = _rows(plane_runtime, owner)
    monkeypatch.setenv(ni.PROFILE_ENV, ni.PERSONAL)
    refused = client.get(url)
    assert (refused.status_code, refused.json()) == (404, {"detail": "Not Found"})
    assert client.get("/share/not-a-token").status_code == 404
    assert _rows(plane_runtime, owner) == before and len(before) == 1


def test_the_share_routes_refuse_through_their_dependency_on_a_personal_node(client, server_identity, monkeypatch):
    monkeypatch.setenv(ni.PROFILE_ENV, ni.PERSONAL)
    headers = server_identity.headers("someone")
    body = {"chat_id": "c1", "scope": "canvas"}
    for response in (
        client.post("/api/share", json=body, headers=headers),
        client.get("/api/share", headers=headers),
        client.delete("/api/share/1", headers=headers),
    ):
        assert (response.status_code, response.json()) == (401, {"detail": "node.bearer_refused"})


def test_chrome_reports_share_off_on_a_personal_node(monkeypatch):
    monkeypatch.setenv(ENV, "true")
    monkeypatch.setitem(flags._flags, "artifact_sharing", True)
    monkeypatch.delenv(ni.PROFILE_ENV, raising=False)
    assert chrome_availability.projection_chrome_availability()["share_enabled"] is True
    monkeypatch.setenv(ni.PROFILE_ENV, ni.PERSONAL)
    assert chrome_availability.projection_chrome_availability()["share_enabled"] is False
    assert chrome_availability.projection_native_chrome_availability({})["share_enabled"] is False


def test_the_boot_warns_once_and_forces_the_variable_off_on_a_personal_node(monkeypatch, caplog):
    monkeypatch.setenv(ni.PROFILE_ENV, ni.PERSONAL)
    monkeypatch.setenv(ENV, "yes")
    with caplog.at_level(logging.WARNING, logger=boot_posture.logger.name):
        assert boot_posture.refuse_snapshot_links_on_personal_node() is True
    warnings = [record for record in caplog.records if ENV in record.getMessage()]
    assert len(warnings) == 1
    import os

    assert os.environ[ENV] == "false"
    from webrender.chrome import component_model

    assert component_model._enabled("artifact_sharing") is False


def test_the_boot_stays_silent_when_unset_or_on_another_profile(monkeypatch, caplog):
    monkeypatch.setenv(ni.PROFILE_ENV, ni.PERSONAL)
    monkeypatch.delenv(ENV, raising=False)
    with caplog.at_level(logging.WARNING, logger=boot_posture.logger.name):
        assert boot_posture.refuse_snapshot_links_on_personal_node() is False
        monkeypatch.delenv(ni.PROFILE_ENV)
        monkeypatch.setenv(ENV, "true")
        assert boot_posture.refuse_snapshot_links_on_personal_node() is False
    import os

    assert os.environ[ENV] == "true"
    assert [record for record in caplog.records if ENV in record.getMessage()] == []


@pytest.mark.asyncio
async def test_node_status_counts_the_legacy_links_of_a_retired_owner(plane_runtime, store, owner, monkeypatch):
    await _mint(store, owner)
    await _mint(store, owner)
    monkeypatch.setenv(ni.PROFILE_ENV, ni.PERSONAL)
    assert await node_mesh._legacy_link_count() == 0
    data = ni.data_dir()
    data.mkdir(parents=True, exist_ok=True)
    (data / "owner.legacy-20261117T000001Z").write_text("mesh." + "a" * 64, encoding="utf-8")
    (data / "owner.legacy-20261117T000002Z").write_text("", encoding="utf-8")
    assert await node_mesh._legacy_link_count() == 0
    (data / "owner.legacy-20261117T000000Z").write_text(owner, encoding="utf-8")
    assert ni.legacy_owners() == [owner]
    before = _rows(plane_runtime, owner)
    assert await node_mesh._legacy_link_count() == 2
    assert _rows(plane_runtime, owner) == before


@pytest.mark.asyncio
async def test_node_status_counts_zero_when_share_storage_is_unbound(monkeypatch):
    monkeypatch.setenv(ni.PROFILE_ENV, ni.PERSONAL)
    data = ni.data_dir()
    data.mkdir(parents=True, exist_ok=True)
    (data / "owner.legacy-20261117T000000Z").write_text("oidc.abc.someone", encoding="utf-8")
    set_share_store(None)
    assert await node_mesh._legacy_link_count() == 0


def test_legacy_owners_tolerates_a_missing_or_unreadable_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path / "absent"))
    assert ni.legacy_owners() == []
    data = tmp_path / "node-unreadable"
    data.mkdir()
    (data / "owner.legacy-1").mkdir()
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(data))
    assert ni.legacy_owners() == []
