"""Exercises real MCP result shapes, owner lookup, credential boundaries and every exposed control schema."""

import base64
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from agents.gaiakeep import catalog, client, mcp_server
from agents.gaiakeep.issue_log import IssueLog
from agents.gaiakeep.remote_client import RemoteCore
from shared.protocol import MCPRequest


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv('GAIAKEEP_CONNECTION_MODE', 'native')
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
    assert len(reply.result['tools']) == 118
    assert all(t['scope'] for t in reply.result['tools'])
    reply = server.process_request(request())
    reply.validate_result_shape()
    assert reply.result['verdict'] == 'ok'
    assert reply.result['result']['job_id'] == 'real-job'
    assert reply.ui_components[0]['type'] == 'card'


@pytest.mark.parametrize('name,verdict,mutation', [
    ('gaiakeep_core_whoami', 'unavailable', False),
    ('gaiakeep_core_repair', 'unconfirmed', True),
])
def test_tool_failure_records_safe_dispatch_state(server, monkeypatch, name, verdict, mutation):
    records = []
    monkeypatch.setattr(server.issue_log, 'record',
                        lambda tool, code, **state: records.append((tool, code, state)))
    def interrupted(*args):
        raise TimeoutError('private-password and private-file-content')
    monkeypatch.setattr(client, 'execute', interrupted)
    reply = server.process_request(request(name))
    assert reply.error['data']['verdict'] == verdict
    assert records == [(name, verdict, {'dispatched': True, 'mutation': mutation})]
    assert 'private-password' not in str(records) + str(reply.error)


def test_preflight_failure_records_without_dispatched_claim(server, monkeypatch):
    records = []
    monkeypatch.setattr(server.issue_log, 'record',
                        lambda tool, code, **state: records.append((tool, code, state)))
    reply = server.process_request(request(params={'unknown-secret-path': 'private-user-content'}))
    assert reply.error['data']['verdict'] == 'invalid_argument'
    assert records == [('gaiakeep_core_whoami', 'invalid_argument', {'dispatched': False, 'mutation': False})]
    assert 'private-user-content' not in str(records)


def test_success_does_not_create_issue(server, monkeypatch):
    monkeypatch.setattr(server.issue_log, 'record', lambda *args, **kwargs: pytest.fail('successful call logged an issue'))
    assert server.process_request(request()).result['verdict'] == 'ok'


def test_actual_failed_tool_appends_redacted_issue(server, monkeypatch, tmp_path):
    directory = tmp_path / 'operator-log'
    monkeypatch.setenv('GAIAKEEP_ISSUE_LOG_DIRECTORY', str(directory))
    server.issue_log = IssueLog()
    def interrupted(*args):
        raise TimeoutError('private-password, private-user-content, /private/patient-path')
    monkeypatch.setattr(client, 'execute', interrupted)
    reply = server.process_request(request())
    assert reply.error['data']['verdict'] == 'unavailable'
    log = (directory / 'ISSUES.md').read_text()
    assert len([line for line in log.splitlines() if line.startswith('- ')]) == 1
    assert 'tool=gaiakeep_core_whoami' in log and 'verdict=unavailable' in log
    assert 'post_dispatch' in log and 'mutation=false' in log
    assert all(value not in log for value in ('private-password', 'private-user-content', 'patient-path', 'ssh-password', 'fabric-secret'))


def test_sink_failure_preserves_uncertain_write_and_request_id(server, monkeypatch, tmp_path, caplog):
    from agents.gaiakeep import issue_log

    monkeypatch.setenv('GAIAKEEP_ISSUE_LOG_DIRECTORY', str(tmp_path / 'operator-log'))
    server.issue_log = IssueLog()
    attempts = []
    def interrupted(core, action, params, *args):
        attempts.append(params['request_id'])
        raise TimeoutError('private-password')
    def unavailable_sink(*args):
        raise OSError('private-log-path')
    monkeypatch.setattr(client, 'execute', interrupted)
    monkeypatch.setattr(issue_log, '_append', unavailable_sink)
    reply = server.process_request(request('gaiakeep_core_repair'))
    assert reply.error['data']['verdict'] == 'unconfirmed'
    assert reply.error['data']['reconciliation']['request_id'] == attempts[0]
    assert len(attempts) == 1
    assert any(record.gaiakeep_issue.get('sink') == 'unavailable' for record in caplog.records)
    assert 'private-log-path' not in caplog.text and 'private-password' not in caplog.text + str(reply.error)


@pytest.mark.parametrize('action', ['core.repair', 'read', 'upload'])
def test_remote_bridge_dispatch_preserves_approved_arguments_and_ids(server, monkeypatch, action):
    monkeypatch.setenv('GAIAKEEP_CONNECTION_MODE', 'ssh')
    calls = []
    core = RemoteCore(SimpleNamespace(), {})
    core.perform = lambda *args: calls.append(args) or ({'data_base64': 'aA==', 'vid': 'v'} if action == 'read'
                                                      else {'status': '10'})
    @contextmanager
    def opened(*args):
        yield core, SimpleNamespace(service_key='', allow_legacy=False)
    monkeypatch.setattr(client, 'open_client', opened)
    args = {'machine_id': 'mine', 'user_id': 'owner', '_credentials': 'stale-encrypted-private-key',
            '_credentials_stale': True, '_credentials_encrypted': True}
    if action == 'upload':
        args.update(collection_id='c', path='p', data_base64='aA==', request_id='stable-request-id')
        name = 'gaiakeep_upload_file'
    elif action == 'read':
        args.update(vid='v', path='p')
        name = 'gaiakeep_read_file'
    else:
        args['params'] = {'request_id': 'stable-request-id'}
        name = 'gaiakeep_core_repair'
    out = server.invoke(name, **args)
    assert out['_data']['verdict'] == 'ok'
    assert calls[0][0] == action
    assert 'machine_id' not in calls[0][1] and 'user_id' not in calls[0][1]
    if action != 'read':
        assert out['_data']['reconciliation']['request_id'] == 'stable-request-id'


@pytest.mark.parametrize('action', sorted(catalog.LOCAL_READ_ACTIONS))
def test_account_discovery_is_read_only_ssh_operation(server, monkeypatch, action):
    assert not catalog.is_mutation('gaiakeep_' + action)
    args = {'machine_id': 'mine', 'params': {}, 'user_id': 'owner'}
    assert server.invoke('gaiakeep_' + action, **args)['_data']['verdict'] == 'unsupported'
    monkeypatch.setenv('GAIAKEEP_CONNECTION_MODE', 'ssh')
    core = RemoteCore(SimpleNamespace(), {})
    calls = []
    core.perform = lambda *args: calls.append(args) or {'default_collection': 'mine'}
    @contextmanager
    def opened(*args):
        yield core, SimpleNamespace(service_key='', allow_legacy=False)
    monkeypatch.setattr(client, 'open_client', opened)
    result = server.invoke('gaiakeep_' + action, **args)
    assert result['_data']['verdict'] == 'ok' and 'reconciliation' not in result['_data']
    assert calls == [(action, {'params': {}}, None)]


@pytest.mark.parametrize('action', ['inspect_dataset', 'upload_dataset', 'download_dataset'])
def test_local_dataset_tools_require_remote_mode_and_preserve_public_arguments(server, monkeypatch, action):
    args = {'machine_id': 'mine', 'dataset_ref': 'run42', 'user_id': 'owner'}
    if action == 'upload_dataset':
        args.update(collection_id='runs', manifest_sha256='a' * 64, request_id='retained_request_identity',
                    expected_head='base-version', prefix='runs/42', note='completed run')
    if action == 'download_dataset':
        args.update(vid='immutable-version')
    name = 'gaiakeep_' + action
    assert server.invoke(name, **args)['_data']['verdict'] == 'unsupported'
    monkeypatch.setenv('GAIAKEEP_CONNECTION_MODE', 'ssh')
    core, calls = RemoteCore(SimpleNamespace(), {}), []
    core.perform = lambda *values: calls.append(values) or {'dataset_ref': 'run42'}
    @contextmanager
    def opened(*values):
        yield core, SimpleNamespace(service_key='', allow_legacy=False)
    monkeypatch.setattr(client, 'open_client', opened)
    out = server.invoke(name, **args)
    assert out['_data']['verdict'] == 'ok'
    payload = {key: value for key, value in args.items() if key not in {'machine_id', 'user_id', 'request_id'}}
    assert calls == [(action, payload, args.get('request_id'))]
    if action == 'upload_dataset':
        assert out['_data']['reconciliation']['manifest_sha256'] == 'a' * 64


def test_successful_pending_publication_is_publishable_with_safe_job_identity(server, monkeypatch):
    monkeypatch.setattr(client, 'upload', lambda *args: {'publication': {'vid': 'version-42', 'commit_job': 'durable-42'}})
    args = {'machine_id': 'mine', 'collection_id': 'runs', 'path': 'p', 'data_base64': 'aA==',
            'request_id': 'retained_request_identity', 'user_id': 'owner'}
    out = server.invoke('gaiakeep_upload_file', **args)
    assert '_error' not in out and out['_data']['verdict'] == 'pending'
    assert out['_data']['reconciliation']['commit_job'] == 'durable-42'
    assert out['_ui_components'][0]['title'] == 'GaiaKeep version awaiting durable copies'
    monkeypatch.setattr(client, 'upload', lambda *args: {'publication': {'vid': 'version-42', 'commit_job': '../bad'}})
    assert server.invoke('gaiakeep_upload_file', **args)['_data']['verdict'] == 'unconfirmed'


@pytest.mark.parametrize('publication,verdict', [
    ({'vid': 'v', 'commit_job': None, 'pending': False}, 'ok'),
    ({'vid': 'v', 'commit_job': None, 'pending': 'false'}, 'ok'),
    ({'vid': 'v', 'commit_job': 'durable-job', 'pending': True}, 'pending'),
    ({'vid': 'v', 'commit_job': 'durable-job', 'pending': False}, 'pending'),
    ({'vid': 'v', 'commit_job': None, 'pending': True}, 'unconfirmed'),
    ({'vid': 'v', 'commit_job': '', 'pending': False}, 'unconfirmed'),
])
def test_sdk_publication_state_never_confuses_null_job_with_pending(server, monkeypatch, publication, verdict):
    calls = []
    monkeypatch.setattr(client, 'upload', lambda *args: calls.append(args) or publication)
    out = server.invoke('gaiakeep_upload_file', machine_id='mine', collection_id='runs', path='p',
                        data_base64='aA==', request_id='retained_request_identity', user_id='owner')
    assert out['_data']['verdict'] == verdict
    assert len(calls) == 1
    assert out['_data']['reconciliation']['request_id'] == 'retained_request_identity'


def test_stage_required_read_reports_recall_without_dispatching_a_stage(server, monkeypatch):
    calls = []
    def offline(core, action, params, *args):
        calls.append(action)
        raise client.AgentError('stage_required', client.STAGE_MESSAGE)
    monkeypatch.setattr(client, 'execute', offline)
    out = server.process_request(request('gaiakeep_core_list', {'vid': 'archived-version'}))
    assert out.error['data']['verdict'] == 'stage_required'
    assert 'gaiakeep_core_stage' in out.error['message']
    assert calls == ['core.list'] and out.error['retryable'] is False
    assert catalog.is_mutation('gaiakeep_core_stage')


def test_pending_read_failure_preserves_job_and_explicit_wait_message(server, monkeypatch):
    def waiting(*args):
        raise client.AgentError('pending', 'This version is awaiting durable copies; check its commit job.',
                                reconciliation={'commit_job': 'durable-42', 'vid': 'version-42'})
    monkeypatch.setattr(client, 'execute', waiting)
    out = server.process_request(request('gaiakeep_core_list', {'vid': 'version-42'}))
    assert out.error['data']['verdict'] == 'pending'
    assert out.error['data']['reconciliation'] == {'commit_job': 'durable-42', 'vid': 'version-42'}
    assert 'awaiting durable copies' in out.error['message']


def test_dataset_mcp_sanitizer_preserves_json_literal_filename_and_manifest_digest(server, monkeypatch):
    from agents.gaiakeep.dataset_workspace import _manifest

    monkeypatch.setenv('GAIAKEEP_CONNECTION_MODE', 'ssh')
    manifest = _manifest([{'path': '["metrics"]', 'size': 1, 'sha256': 'a' * 64}])
    core = RemoteCore(SimpleNamespace(), {})
    core.perform = lambda *values: manifest
    @contextmanager
    def opened(*values):
        yield core, SimpleNamespace(service_key='', allow_legacy=False)
    monkeypatch.setattr(client, 'open_client', opened)
    result = server.invoke('gaiakeep_inspect_dataset', machine_id='mine', dataset_ref='run42', user_id='owner')
    assert result['_data']['result'] == manifest and result['_data']['result']['files'][0]['path'] == '["metrics"]'


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
    def interrupted(core, collection_id, path, data_base64, branch, strategy, base_vid, expected_head, request_id, note):
        calls.append(request_id)
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
