"""Exercises real MCP result shapes, owner lookup, credential boundaries and every exposed control schema."""

import base64
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from agents.gaiakeep import catalog, client, mcp_server
from shared.protocol import MCPRequest


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setitem(mcp_server.flags._flags, 'gaiakeep', True)
    monkeypatch.setitem(mcp_server.flags._flags, 'cresco', True)
    monkeypatch.setattr(mcp_server.remote_machines, 'build_target',
                        lambda *args: SimpleNamespace(secret='ssh-password', passphrase=None))
    core = SimpleNamespace(call=lambda *args, **kwargs: {'status': '10', 'job_id': 'real-job'})
    config = SimpleNamespace(service_key='fabric-secret', allow_legacy=False)
    @contextmanager
    def opened(*args):
        yield core, config
    monkeypatch.setattr(client, 'open_client', opened)
    return mcp_server.MCPServer(object(), object())


def request(name='gaiakeep_core_whoami', params=None, **context):
    return MCPRequest(request_id='req', method='tools/call',
                      params={'name': name, 'arguments': dict(machine_id='mine', params=params or {},
                                                             user_id='owner', _credentials={}, **context)})


def test_list_and_result_shape(server):
    reply = server.process_request(MCPRequest(method='tools/list'))
    assert len(reply.result['tools']) == 109
    assert all(t['scope'] for t in reply.result['tools'])
    reply = server.process_request(request())
    reply.validate_result_shape()
    assert reply.result['verdict'] == 'ok'
    assert reply.result['result']['job_id'] == 'real-job'
    assert reply.ui_components[0]['type'] == 'card'


@pytest.mark.parametrize('method,params', [('other', {}), ('tools/call', {}),
                                         ('tools/call', {'name': 'gaiakeep_core_whoami', 'arguments': []})])
def test_bad_request(server, method, params):
    reply = server.process_request(MCPRequest(method=method, params=params))
    reply.validate_result_shape()
    assert reply.error['retryable'] is False


def test_identity_cannot_override_owner(server, monkeypatch):
    captured = []
    monkeypatch.setattr(mcp_server.remote_machines, 'build_target',
                        lambda *args: captured.append(args[2]) or SimpleNamespace(secret='', passphrase=None))
    req = request()
    req.params['arguments']['user_id'] = 'model-spoof'
    mcp_server.bind_owner_context(req.params['arguments'], 'authenticated-owner')
    assert server.process_request(req).error is None
    assert captured == ['authenticated-owner']


@pytest.mark.parametrize('change', [{'user_id': None}, {'_credentials_encrypted': True},
                                    {'_credentials_stale': True}, {'_credentials': 'invalid'},
                                    {'params': {'audience': 'spoof'}}, {'confirmed': True}])
def test_preflight_denial(server, change):
    req = request()
    req.params['arguments'].update(change)
    response = server.process_request(req)
    response.validate_result_shape()
    assert response.error and response.error['retryable'] is False


def test_flags_off(server, monkeypatch):
    monkeypatch.setitem(mcp_server.flags._flags, 'cresco', False)
    assert server.process_request(request()).error['data']['verdict'] == 'not_configured'


def test_owner_scoped_lookup(server, monkeypatch):
    from orchestrator.remote_machines import MachineNotFound
    def denied(*args):
        assert args[2] == 'owner'
        assert args[3] == 'mine'
        raise MachineNotFound('private-owner-information')
    monkeypatch.setattr(mcp_server.remote_machines, 'build_target', denied)
    reply = server.process_request(request())
    assert reply.error
    assert 'private-owner-information' not in str(reply.error)


@pytest.mark.parametrize('name,params', [
    ('gaiakeep_listnodes', {}),
    ('gaiakeep_core_legalorder', {'tenant_id': 't', 'order_ref': 'o', 'authority': 'a', 'action': 'shorten'}),
])
def test_unsupported_before_dispatch(server, name, params):
    assert server.process_request(request(name, params)).error['data']['verdict'] == 'unsupported'


def test_unknown_mutation_is_not_retried(server, monkeypatch):
    attempts = []
    def interrupted(core, action, params, *args):
        attempts.append(params['request_id'])
        raise TimeoutError('fabric-secret')
    monkeypatch.setattr(client, 'execute', interrupted)
    out = server.process_request(request('gaiakeep_core_repair'))
    assert out.error['data']['verdict'] == 'unconfirmed'
    assert len(attempts) == 1
    assert out.error['data']['reconciliation']['request_id'] == attempts[0]
    assert 'fabric-secret' not in str(out.error)


def test_legacy_fetch_never_opens_a_connection(server, monkeypatch):
    def forbidden(*a):
        pytest.fail('incomplete fetch reached a transport')
    monkeypatch.setattr(client, 'open_client', forbidden)
    params = {}
    response = server.process_request(request('gaiakeep_fetch', params))
    assert response.error['data']['verdict'] == 'unsupported'


def test_upload_timeout_retains_exact_ingest_identity(server, monkeypatch):
    calls = []
    def interrupted(*args):
        calls.append(args[-1])
        raise TimeoutError()
    monkeypatch.setattr(client, 'upload', interrupted)
    req = request()
    req.params = {'name': 'gaiakeep_upload_file', 'arguments': {
        'machine_id': 'mine', 'path': 'file', 'collection_id': 'c', 'data_base64': '', 'user_id': 'owner'}}
    response = server.process_request(req)
    assert response.error['data']['verdict'] == 'unconfirmed'
    assert response.error['data']['reconciliation']['request_id'] == calls[0]
    assert response.error['data']['reconciliation']['collection_id'] == 'c'


def test_sdk_value_error_after_mutation_is_unconfirmed(server, monkeypatch):
    def invalid_native_number(*a):
        raise ValueError('malformed native publication number')
    monkeypatch.setattr(client, 'execute', invalid_native_number)
    response = server.process_request(request('gaiakeep_core_repair'))
    assert response.error['data']['verdict'] == 'unconfirmed'
    assert 'malformed native' not in response.error['message']


def test_post_mutation_response_failure(server, monkeypatch):
    def bad(*args, **kwargs):
        raise client.AgentError('protocol_error', 'internal-frame')
    monkeypatch.setattr(client, 'execute', bad)
    assert server.process_request(request('gaiakeep_core_repair')).error['data']['verdict'] == 'unconfirmed'


def test_invalid_profile_reply_is_unconfirmed_after_dispatch(server, monkeypatch):
    core = SimpleNamespace(call=lambda *a, **kw: {'profiles': '{invalid'})
    config = SimpleNamespace(service_key='fabric-secret', allow_legacy=False)
    @contextmanager
    def opened(*a):
        yield core, config
    monkeypatch.setattr(client, 'open_client', opened)
    response = server.process_request(request('gaiakeep_core_profile'))
    assert response.error['data']['verdict'] == 'unconfirmed'


def test_response_size_bound(server, monkeypatch):
    monkeypatch.setattr(client, 'execute', lambda *a: {'large': 'x' * (client.MAX_RPC + 1)})
    assert server.process_request(request()).error['data']['verdict'] == 'protocol_error'


def test_read_and_upload_surfaces(server, monkeypatch):
    monkeypatch.setattr(client, 'read', lambda *a: {'data_base64': base64.b64encode(b'hello').decode(), 'byte_count': 5})
    req = request()
    req.params = {'name': 'gaiakeep_read_file', 'arguments': {'machine_id': 'mine', 'path': 'p', 'vid': 'v', 'user_id': 'owner'}}
    assert server.process_request(req).result['result']['byte_count'] == 5
    monkeypatch.setattr(client, 'upload', lambda *a: {'vid': 'v'})
    req.params = {'name': 'gaiakeep_upload_file', 'arguments': {'machine_id': 'mine', 'path': 'p', 'collection_id': 'c',
                                                             'data_base64': '', 'user_id': 'owner'}}
    assert server.process_request(req).result['result']['vid'] == 'v'


@pytest.mark.parametrize('name', [n for n in catalog.TOOLS if catalog.TOOLS[n]['action'] in catalog.ACTIONS])
def test_every_public_control_schema(name):
    properties = catalog.TOOLS[name]['input_schema']['properties']['params']['properties']
    required = catalog.TOOLS[name]['input_schema']['properties']['params']['required']
    params = {k: (0 if properties[k].get('type') == 'integer' else
                  True if properties[k].get('type') == 'boolean' else 'native-id') for k in required}
    if 'request_id' in params:
        params['request_id'] = 'a' * 32
    catalog.validate(name, {'machine_id': 'mine', 'params': params})
