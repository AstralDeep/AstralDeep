"""Checks that only the personal profile boots: every other profile value exits 78 with node.profile_removed before the orchestrator, the Plane or any data directory is touched."""

from __future__ import annotations

import logging

import pytest

from orchestrator import boot_posture, orchestrator as orchestrator_module
from shared import node_identity


@pytest.mark.parametrize("profile", [None, "", "server", "institution", "Personal ", "PERSONAL", "personal "])
def test_every_other_profile_exits_78_without_opening_anything(profile, tmp_path, monkeypatch, caplog):
    data_dir = tmp_path / "node"
    monkeypatch.setenv(node_identity.DATA_DIR_ENV, str(data_dir))
    if profile is None:
        monkeypatch.delenv(node_identity.PROFILE_ENV, raising=False)
    else:
        monkeypatch.setenv(node_identity.PROFILE_ENV, profile)
    constructed = []
    monkeypatch.setattr(orchestrator_module, "Orchestrator", lambda *a, **k: constructed.append(1))
    with caplog.at_level(logging.CRITICAL), pytest.raises(SystemExit) as exited:
        orchestrator_module.main()
    assert exited.value.code == 78
    assert boot_posture.PROFILE_REMOVED in caplog.text and "No data was read or changed" in caplog.text
    assert constructed == [] and not data_dir.exists()


def test_the_personal_profile_passes_the_profile_check(monkeypatch):
    monkeypatch.setenv(node_identity.PROFILE_ENV, node_identity.PERSONAL)
    boot_posture.refuse_removed_profile()
    assert node_identity.is_personal()


def test_the_removed_profile_helpers_are_gone():
    for name in ("is_institution", "canonical_owner_id", "accepted_issuers", "issuer_digest", "INSTITUTION", "ISSUERS_ENV"):
        assert not hasattr(node_identity, name), name
