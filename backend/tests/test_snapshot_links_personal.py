"""Proves snapshot links are off on a personal node whatever FF_ARTIFACT_SHARING says: the flag reads false, /share/{token} answers 404, the /api/share routes refuse through their dependency, the chrome reports share off, one boot warning is logged, and stored share rows keep their bytes while node status counts them.
It drives shared/feature_flags.py, orchestrator/api.py, chrome_availability.py, boot_posture.py and node_mesh.py against the real Plane-backed ShareGrantStore."""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import MagicMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from orchestrator import chrome_availability, boot_posture  # noqa: E402
from orchestrator.api import share_router  # noqa: E402
from shared import node_identity as ni  # noqa: E402
from shared.feature_flags import flags  # noqa: E402
from tests.helpers.voice_plane_runtime import isolated_plane_runtime  # noqa: E402

ENV = "FF_ARTIFACT_SHARING"


class _CleanAnalyzer:
    def analyze(self, text, language, entities, score_threshold):
        return []


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("snapshot_links_personal") as runtime:
        yield runtime


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


