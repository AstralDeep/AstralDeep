"""Feature 063 — the defensive guards on the small cross-module deltas.

Each 063 entry point outside the verb libraries carries one guard the behaviour
suites never trip, because it fires only when a dependency is broken: a flag
helper that raises, an older schema missing a table, a sign-out with no
principal. Those guards decide whether a partial failure degrades or takes down
a boot / a sign-out, so each is pinned to the direction it must degrade —
fail-CLOSED for the feature's visibility, fail-OPEN for the surrounding flow.
Hermetic: no DB connection, no network, no agent instantiation.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


def _unavailable(*_a, **_k):
    raise RuntimeError("feature-flag store unavailable")


# ── local_agents: a broken flag check must not take down the boot ──────────────

async def test_register_built_ins_survives_a_failing_remote_flag_check(monkeypatch):
    from orchestrator import local_agents
    from shared.feature_flags import flags

    monkeypatch.setattr(flags, "is_enabled", _unavailable)
    monkeypatch.setattr(local_agents, "discover_built_in_agent_dirs", lambda *a, **k: [])
    monkeypatch.setattr(local_agents, "_load_agent_class",
                        lambda d: pytest.fail("loaded the 063 agent after the flag check failed"))
    orch = SimpleNamespace(local_agents={}, register_agent=None)
    # Fail-closed on the remote agent (it is never loaded), but the fleet
    # registration itself still returns normally.
    assert await local_agents.register_built_ins(orch) == []


# ── menu_model: an unreadable flag hides the surface (fail-closed) ─────────────

def test_remote_machines_menu_item_is_absent_when_the_flag_cannot_be_read(monkeypatch):
    from shared.feature_flags import flags
    from webrender.chrome import menu_model

    monkeypatch.setattr(flags, "is_enabled", _unavailable)
    model = menu_model.menu_model_dict(roles=["user", "admin"])
    assert "remote_machines" not in json.dumps(model)


# ── database: an older schema missing a table must not abort the migration ─────

def test_merged_remote_agent_cleanup_tolerates_a_missing_table():
    from shared.database import Database

    class _Cursor:
        def __init__(self):
            self.tables = []

        def execute(self, sql, params=None):
            self.tables.append(sql.split("FROM ")[1].split()[0])
            if self.tables[-1] == "agent_ownership":  # first table of the sweep
                raise RuntimeError('relation "agent_ownership" does not exist')

    cursor = _Cursor()
    # Unbound call: the migration reads only the class-level retired-id tuple, so
    # it needs no Database instance and no live connection.
    Database._cleanup_merged_remote_agents_063(
        SimpleNamespace(_RETIRED_AGENT_IDS_063=Database._RETIRED_AGENT_IDS_063), cursor)
    # The failure of one table does not skip the rest of the sweep.
    assert cursor.tables == ['agent_ownership', 'agent_scopes', 'tool_overrides',
                             'tool_permissions', 'user_credentials', 'agent_trust']


# ── web_auth: a sign-out with no principal touches nothing ─────────────────────

async def test_machine_credential_destruction_is_a_noop_without_a_principal(monkeypatch):
    import shared.database as db_mod
    from orchestrator import web_auth

    monkeypatch.setattr(db_mod, "Database",
                        lambda *a, **k: pytest.fail("opened a connection with no principal"))
    assert await web_auth._destroy_machine_credentials("", "logout") is None
