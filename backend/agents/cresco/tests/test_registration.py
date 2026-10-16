"""Pins the cresco registration gate in orchestrator/local_agents.py: the agent loads and
registers only when FF_CRESCO is on, the GaiaKeep dual-flag posture is unchanged, and the
agent id is publicly cataloged.
"""

from types import SimpleNamespace

import pytest
from shared.protocol import AgentCard

from orchestrator import local_agents


class _FakeAgent:
    def __init__(self, agent_id):
        self.card = AgentCard(name=agent_id, description="fake", agent_id=agent_id)


def _registration(monkeypatch, enabled_flags):
    from shared.feature_flags import flags

    monkeypatch.setattr(flags, "is_enabled", lambda name: name in enabled_flags)
    attempted = []
    monkeypatch.setattr(local_agents, "discover_built_in_agent_dirs", lambda *a, **k: [])
    monkeypatch.setattr(
        local_agents,
        "_load_agent_class",
        lambda d: attempted.append(d) or (lambda *a, **k: _FakeAgent(f"{d}-1")),
    )
    from shared import attachment_materializer, attachment_resolver

    monkeypatch.setattr(attachment_resolver, "register_plane_runtime", lambda *a: True)
    monkeypatch.setattr(attachment_materializer, "register_materialization_service", lambda *a: None)

    async def _register(ws, msg):
        return None

    plane = SimpleNamespace(
        runtime=object(), repositories=object(), blobs=object(), attachment_materializer=object()
    )
    orch = SimpleNamespace(
        local_agents={}, register_agent=_register, runtime_composition=SimpleNamespace(plane=plane)
    )
    return orch, attempted


@pytest.mark.asyncio
async def test_cresco_registers_only_when_flag_is_on(monkeypatch):
    orch, attempted = _registration(monkeypatch, {"cresco"})
    registered = await local_agents.register_built_ins(orch)
    assert attempted == ["cresco"]
    assert registered == ["cresco-1"]
    assert "cresco-1" in orch.local_agents


@pytest.mark.asyncio
async def test_flag_off_never_loads_or_dials_cresco(monkeypatch):
    orch, attempted = _registration(monkeypatch, set())
    assert await local_agents.register_built_ins(orch) == []
    assert attempted == []
    assert orch.local_agents == {}


@pytest.mark.asyncio
async def test_gaiakeep_dual_flag_posture_is_preserved(monkeypatch):
    orch, attempted = _registration(monkeypatch, {"cresco", "gaiakeep"})
    await local_agents.register_built_ins(orch)
    assert "gaiakeep" in attempted and "cresco" in attempted

    orch, attempted = _registration(monkeypatch, {"gaiakeep"})
    await local_agents.register_built_ins(orch)
    assert "gaiakeep" not in attempted and "cresco" not in attempted

    orch, attempted = _registration(monkeypatch, {"cresco"})
    await local_agents.register_built_ins(orch)
    assert "cresco" in attempted and "gaiakeep" not in attempted


def test_cresco_agent_id_is_publicly_cataloged():
    assert "cresco-1" in local_agents.FIRST_PARTY_PUBLIC_AGENT_IDS
    assert "cresco" not in local_agents.discover_built_in_agent_dirs()


def test_fabric_results_retain_untrusted_content_taint():
    from orchestrator.taint import _UNTRUSTED_AGENTS

    assert "cresco-1" in _UNTRUSTED_AGENTS


def test_cresco_agent_card_keeps_read_skills_and_tiered_effects(monkeypatch):
    from agents.cresco.cresco_agent import CrescoAgent
    from shared.base_agent import BaseA2AAgent

    monkeypatch.setattr(
        BaseA2AAgent, "_init_crypto", lambda self: setattr(self, "_public_key_jwk", {"test": "ephemeral"})
    )
    agent = CrescoAgent(port=8998)
    assert agent.card.agent_id == "cresco-1"
    scopes = {skill.name: skill.scope for skill in agent.card.skills}
    assert {name for name, scope in scopes.items() if scope == "tools:read"} == {
        "cresco_list_regions",
        "cresco_list_agents",
        "cresco_agent_info",
        "cresco_get_sysinfo",
    }
    assert scopes["cresco_dispatch_work"] == "tools:write"
    assert scopes["cresco_run_command"] == "tools:system"


def test_standalone_start_refuses_when_the_flag_is_off(monkeypatch):
    from agents.cresco.cresco_agent import DISABLED_EXIT_CODE, main
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "cresco", False)
    with pytest.raises(SystemExit) as stopped:
        main()
    assert stopped.value.code == DISABLED_EXIT_CODE


def test_standalone_start_runs_the_agent_when_enabled(monkeypatch):
    from unittest.mock import AsyncMock

    import agents.cresco.cresco_agent as cresco_agent_module
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "cresco", True)
    monkeypatch.setattr("sys.argv", ["cresco_agent.py", "--port", "8999"])
    started = {}
    run = AsyncMock()
    monkeypatch.setattr(
        cresco_agent_module,
        "CrescoAgent",
        lambda port=None: started.update(port=port) or SimpleNamespace(run=run),
    )
    cresco_agent_module.main()
    assert started == {"port": 8999} and run.await_count == 1


def test_startup_gate_follows_the_flag(monkeypatch):
    import start
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "cresco", False)
    assert start._cresco_enabled() is False
    monkeypatch.setitem(flags._flags, "cresco", True)
    assert start._cresco_enabled() is True


def test_startup_gate_fails_closed(monkeypatch):
    import start
    from shared.feature_flags import flags

    def unavailable(name):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(flags, "is_enabled", unavailable)
    assert start._cresco_enabled() is False


@pytest.mark.parametrize(("enabled", "inprocess", "spawned"), [
    (False, False, False), (False, True, False), (True, True, False), (True, False, True),
])
def test_subprocess_spawn_respects_flag_and_transport_mode(monkeypatch, tmp_path, enabled, inprocess, spawned):
    import start
    from tests.test_start_wait import _agents_tree, _run_main

    backend, agents = _agents_tree(tmp_path)
    directory = agents / "cresco"
    directory.mkdir()
    (directory / "cresco_agent.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(start, "_cresco_enabled", lambda: enabled)
    _, started = _run_main(monkeypatch, backend, inprocess=inprocess, remote_flag=False)
    assert ("cresco" in started) is spawned


async def test_cresco_never_receives_implicit_safe_permissions(monkeypatch):
    from tests.test_remote_orchestrator_wiring_063 import _drive_start

    _, seeded = await _drive_start(monkeypatch, remote_compute=False, gaiakeep=True)
    assert "cresco-1" not in seeded[0][1]
    assert "gaiakeep-1" not in seeded[0][1]


async def test_registration_survives_a_failing_cresco_flag_check(monkeypatch):
    from shared.feature_flags import flags

    def _boom(name):
        if name == "cresco":
            raise RuntimeError("flag source unavailable")
        return False

    monkeypatch.setattr(flags, "is_enabled", _boom)
    monkeypatch.setattr(local_agents, "discover_built_in_agent_dirs", lambda *a, **k: [])
    from shared import attachment_materializer, attachment_resolver

    monkeypatch.setattr(attachment_resolver, "register_plane_runtime", lambda *a: True)
    monkeypatch.setattr(attachment_materializer, "register_materialization_service", lambda *a: None)

    async def _register(ws, msg):
        return None

    plane = SimpleNamespace(
        runtime=object(), repositories=object(), blobs=object(), attachment_materializer=object()
    )
    orch = SimpleNamespace(
        local_agents={}, register_agent=_register, runtime_composition=SimpleNamespace(plane=plane)
    )
    assert await local_agents.register_built_ins(orch) == []
    assert orch.local_agents == {}
