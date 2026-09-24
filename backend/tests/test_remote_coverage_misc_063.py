"""Tests for small cross-module guards outside the main remote-machine verb libraries
(local_agents.py, web_auth.py, chrome/menu_model.py): a failing remote flag check
degrades the fleet/menu safely, and credential destruction needs a principal.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


def _unavailable(*_a, **_k):
    raise RuntimeError("feature-flag store unavailable")


async def test_register_built_ins_survives_a_failing_remote_flag_check(monkeypatch):
    from orchestrator import local_agents
    from shared.feature_flags import flags
    from shared import attachment_materializer, attachment_resolver

    monkeypatch.setattr(flags, "is_enabled", _unavailable)
    monkeypatch.setattr(local_agents, "discover_built_in_agent_dirs", lambda *a, **k: [])
    monkeypatch.setattr(local_agents, "_load_agent_class",
                        lambda d: pytest.fail("loaded the 063 agent after the flag check failed"))
    monkeypatch.setattr(
        attachment_resolver,
        "register_plane_runtime",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        attachment_materializer,
        "register_materialization_service",
        lambda *_args: None,
    )
    plane = SimpleNamespace(
        runtime=object(),
        repositories=object(),
        blobs=object(),
        attachment_materializer=object(),
    )
    orch = SimpleNamespace(
        local_agents={},
        register_agent=None,
        runtime_composition=SimpleNamespace(plane=plane),
    )
    assert await local_agents.register_built_ins(orch) == []


def test_remote_machines_menu_item_is_absent_when_the_flag_cannot_be_read(monkeypatch):
    from shared.feature_flags import flags
    from webrender.chrome import menu_model

    monkeypatch.setattr(flags, "is_enabled", _unavailable)
    model = menu_model.menu_model_dict(roles=["user", "admin"])
    assert "remote_machines" not in json.dumps(model)


async def test_machine_credential_destruction_is_a_noop_without_a_principal(monkeypatch):
    from orchestrator import web_auth

    manager = SimpleNamespace(
        remove_machine_credentials_for_user=lambda *_args: pytest.fail(
            "credential persistence was touched without a principal"
        )
    )
    monkeypatch.setattr(web_auth, "_CREDENTIAL_MANAGER", manager)
    assert await web_auth._destroy_machine_credentials("", "logout") is None
