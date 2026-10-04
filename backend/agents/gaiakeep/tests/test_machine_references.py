"""Verifies owner-scoped read references and stable machine identities for Gaia mutations through MCP dispatch."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agents.gaiakeep import client, mcp_server
from agents.gaiakeep.remote_client import RemoteCore
from orchestrator.credential_manager import CredentialNotConfigured
from orchestrator.remote_machines import MachineNotFound
from shared.protocol import MCPRequest


@pytest.fixture
def machine_server(monkeypatch):
    calls = []
    target = SimpleNamespace(secret='', passphrase=None)
    monkeypatch.setenv('GAIAKEEP_CONNECTION_MODE', 'ssh')
    monkeypatch.setitem(mcp_server.flags._flags, 'gaiakeep', True)
    monkeypatch.setitem(mcp_server.flags._flags, 'cresco', True)
    server = mcp_server.MCPServer(object(), object())
    monkeypatch.setattr(server.issue_log, 'record', lambda *args, **kwargs: None)

    def build(source, credentials, owner, reference):
        calls.append(('build', owner, reference))
        if owner != 'owner' or reference != 'canonical-id':
            raise MachineNotFound(reference)
        return target

    def resolve(source, owner, reference):
        calls.append(('resolve', owner, reference))
        if owner == 'owner' and reference in {'DGX', 'dgx', '203.0.113.10'}:
            return {'machine_id': 'canonical-id'}
        return None

    core = RemoteCore(target, {})

    def perform(*args):
        calls.append(('perform', args[0]))
        return {'status': 'ok'}

    core.perform = perform

    @contextmanager
    def opened(*args):
        calls.append(('open',))
        yield core, SimpleNamespace(service_key='', allow_legacy=False)

    monkeypatch.setattr(mcp_server.remote_machines, 'build_target', build)
    monkeypatch.setattr(mcp_server.remote_machines, 'resolve_machine', resolve)
    monkeypatch.setattr(client, 'open_client', opened)
    return server, calls, target


def invoke(server, reference, owner='owner', tool='gaiakeep_core_status'):
    arguments = {'machine_id': reference, 'params': {}, 'user_id': owner}
    request = MCPRequest(method='tools/call', params={'name': tool, 'arguments': arguments})
    response = server.process_request(request)
    response.validate_result_shape()
    assert arguments == {'machine_id': reference, 'params': {}, 'user_id': owner}
    return response


@pytest.mark.parametrize('reference', ['canonical-id', 'DGX', 'dgx', '203.0.113.10'])
@pytest.mark.parametrize('tool', ['gaiakeep_core_status', 'gaiakeep_connection_info'])
def test_read_resolves_only_within_authenticated_owner(machine_server, reference, tool):
    server, calls, _ = machine_server
    response = invoke(server, reference, tool=tool)
    assert response.error is None and response.result['verdict'] == 'ok'
    assert calls[0] == ('build', 'owner', reference)
    if reference == 'canonical-id':
        assert not any(call[0] == 'resolve' for call in calls)
    else:
        assert calls[1:3] == [('resolve', 'owner', reference), ('build', 'owner', 'canonical-id')]
    assert sum(call[0] == 'perform' for call in calls) == 1


@pytest.mark.parametrize('owner,reference', [('owner', 'missing'), ('other', 'canonical-id'), ('other', 'DGX')])
def test_missing_and_foreign_references_stop_before_transport(machine_server, owner, reference):
    server, calls, _ = machine_server
    response = invoke(server, reference, owner)
    assert response.error['data']['verdict'] == 'invalid_argument'
    assert response.error['retryable'] is False
    assert calls == [('build', owner, reference), ('resolve', owner, reference)]


@pytest.mark.parametrize('reference', ['DGX', 'dgx', '203.0.113.10'])
def test_mutations_do_not_resolve_mutable_aliases(machine_server, reference):
    server, calls, _ = machine_server
    response = invoke(server, reference, tool='gaiakeep_core_repair')
    assert response.error['data']['verdict'] == 'invalid_argument'
    assert response.error['retryable'] is False
    assert 'registered machine ID' in response.error['message']
    assert calls == [('build', 'owner', reference)]


def test_canonical_mutation_keeps_target_identity(machine_server):
    server, calls, _ = machine_server
    response = invoke(server, 'canonical-id', tool='gaiakeep_core_repair')
    assert response.error is None
    assert not any(call[0] == 'resolve' for call in calls)
    assert calls[0] == ('build', 'owner', 'canonical-id')
    assert sum(call[0] == 'perform' for call in calls) == 1


def test_credential_failure_does_not_fall_back_to_another_machine(machine_server, monkeypatch):
    server, calls, _ = machine_server

    def unavailable(*args):
        raise CredentialNotConfigured('private-credential-information')

    monkeypatch.setattr(mcp_server.remote_machines, 'build_target', unavailable)
    response = invoke(server, 'canonical-id')
    assert response.error['data']['verdict'] == 'unavailable'
    assert 'private-credential-information' not in str(response.error)
    assert calls == []


def test_deletion_after_resolution_stops_before_transport(machine_server, monkeypatch):
    server, calls, _ = machine_server

    def deleted(source, credentials, owner, reference):
        calls.append(('build', owner, reference))
        raise MachineNotFound(reference)

    monkeypatch.setattr(mcp_server.remote_machines, 'build_target', deleted)
    response = invoke(server, 'DGX')
    assert response.error['data']['verdict'] == 'unavailable'
    assert calls == [('build', 'owner', 'DGX'), ('resolve', 'owner', 'DGX'), ('build', 'owner', 'canonical-id')]
