"""Exercises agent card construction, Plane binding, feature flags and standalone composition without persisted test keys."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agents.gaiakeep import gaiakeep_agent
from shared.feature_flags import FeatureFlags


def test_agent_requires_initialized_plane():
    with pytest.raises(RuntimeError):
        gaiakeep_agent.GaiakeepAgent(plane_runtime=None)


def test_card_and_plane_bindings(monkeypatch):
    captured = []
    monkeypatch.setattr(gaiakeep_agent, 'CredentialManager', lambda **kw: captured.append(kw) or object())
    monkeypatch.setattr(gaiakeep_agent.BaseA2AAgent, '_init_crypto',
                        lambda self: setattr(self, '_public_key_jwk', {'test': 'ephemeral'}))
    runtime, repos, blobs = object(), object(), object()
    agent = gaiakeep_agent.GaiakeepAgent(port=8998, plane_runtime=runtime, plane_repositories=repos, plane_blobs=blobs)
    assert agent.card.agent_id == 'gaiakeep-1'
    assert agent.host == '127.0.0.1'
    assert len(agent.card.skills) == 111
    assert agent.card.metadata['required_credentials'] == []
    assert captured[0]['plane_runtime'] is runtime
    assert agent.mcp_server.plane_source.plane_repositories is repos


@pytest.mark.parametrize('gaia,cresco,expected', [(False, False, False), (True, False, False),
                                               (False, True, False), (True, True, True)])
def test_two_flags_and_startup(monkeypatch, gaia, cresco, expected):
    import start
    from shared.feature_flags import flags
    monkeypatch.setitem(flags._flags, 'gaiakeep', gaia)
    monkeypatch.setitem(flags._flags, 'cresco', cresco)
    assert start._gaiakeep_enabled() is expected
    monkeypatch.delenv('FF_GAIAKEEP', raising=False)
    monkeypatch.delenv('FF_CRESCO', raising=False)
    flags = FeatureFlags()
    assert flags.is_enabled('gaiakeep') is False
    assert flags.is_enabled('cresco') is False


def test_standalone_disabled(monkeypatch):
    from shared.feature_flags import flags
    monkeypatch.setitem(flags._flags, 'gaiakeep', False)
    with pytest.raises(SystemExit) as error:
        gaiakeep_agent.main()
    assert error.value.code == 78


def test_startup_flag_failure_is_disabled(monkeypatch):
    import start
    from shared.feature_flags import flags
    def unavailable(name):
        raise RuntimeError('unavailable')
    monkeypatch.setattr(flags, 'is_enabled', unavailable)
    assert start._gaiakeep_enabled() is False


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [True, False])
async def test_gaia_does_not_receive_implicit_safe_permissions(monkeypatch, enabled):
    from tests.test_remote_orchestrator_wiring_063 import _drive_start
    _, seeded = await _drive_start(monkeypatch, remote_compute=False, gaiakeep=enabled)
    assert 'gaiakeep-1' not in seeded[0][1]


@pytest.mark.parametrize('fail', [False, True])
def test_standalone_composition_cleanup(monkeypatch, fail):
    from orchestrator import plane_composition
    from shared.feature_flags import flags
    monkeypatch.setitem(flags._flags, 'gaiakeep', True)
    monkeypatch.setitem(flags._flags, 'cresco', True)
    monkeypatch.setattr(sys, 'argv', ['gaiakeep_agent.py', '--port', '8998'])
    closed = []
    composition = SimpleNamespace(runtime=object(), repositories=object(), blobs=object(), close=lambda: closed.append(True))
    monkeypatch.setattr(plane_composition, 'compose_plane_from_environment', lambda path: composition)
    run = AsyncMock(side_effect=RuntimeError('failure') if fail else None)
    monkeypatch.setattr(gaiakeep_agent, 'GaiakeepAgent', lambda **kw: SimpleNamespace(run=run))
    if fail:
        with pytest.raises(RuntimeError):
            gaiakeep_agent.main()
    else:
        gaiakeep_agent.main()
    assert closed == [True]


@pytest.mark.parametrize('enabled,inprocess,spawned', [(False, False, False), (False, True, False),
                                                     (True, True, False), (True, False, True)])
def test_spawn_respects_flag_and_transport_mode(monkeypatch, tmp_path, enabled, inprocess, spawned):
    import start
    from tests.test_start_wait import _agents_tree, _run_main
    backend, agents = _agents_tree(tmp_path)
    directory = agents / 'gaiakeep'
    directory.mkdir()
    (directory / 'gaiakeep_agent.py').write_text('', encoding='utf-8')
    monkeypatch.setattr(start, '_gaiakeep_enabled', lambda: enabled)
    _, started = _run_main(monkeypatch, backend, inprocess=inprocess, remote_flag=False)
    assert ('gaiakeep' in started) is spawned


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [True, False])
async def test_normal_builtin_registration(monkeypatch, enabled):
    from orchestrator import local_agents
    from shared import attachment_materializer, attachment_resolver
    from shared.feature_flags import flags
    monkeypatch.setitem(flags._flags, 'gaiakeep', enabled)
    monkeypatch.setitem(flags._flags, 'cresco', enabled)
    monkeypatch.setitem(flags._flags, 'remote_compute', False)
    monkeypatch.setitem(flags._flags, 'computer_use', False)
    monkeypatch.setattr(local_agents, 'discover_built_in_agent_dirs', list)
    monkeypatch.setattr(attachment_resolver, 'register_plane_runtime', lambda *a: True)
    monkeypatch.setattr(attachment_materializer, 'register_materialization_service', lambda *a: None)
    monkeypatch.setattr(gaiakeep_agent, 'CredentialManager', lambda **kw: object())
    monkeypatch.setattr(gaiakeep_agent.BaseA2AAgent, '_init_crypto',
                        lambda self: setattr(self, '_public_key_jwk', {'test': 'ephemeral'}))
    plane = SimpleNamespace(runtime=object(), repositories=object(), blobs=object(), attachment_materializer=object())
    orch = SimpleNamespace(runtime_composition=SimpleNamespace(plane=plane), local_agents={}, register_agent=AsyncMock())
    actual = await local_agents.register_built_ins(orch)
    assert actual == (['gaiakeep-1'] if enabled else [])
    assert orch.register_agent.await_count == int(enabled)
