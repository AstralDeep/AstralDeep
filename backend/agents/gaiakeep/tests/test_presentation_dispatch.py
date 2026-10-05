"""Exercises Gaia presentation through real MCP dispatch and Projection rendering. Existing dispatch fixtures isolate transport while canonical results, reconciliation and approved arguments remain observable."""

import base64
import hashlib
import json
from contextlib import contextmanager
from copy import deepcopy
from html.parser import HTMLParser
from types import SimpleNamespace

from agents.gaiakeep import client
from agents.gaiakeep.remote_client import RemoteCore
from agents.gaiakeep.tests import test_dispatch as dispatch_fixtures

server = dispatch_fixtures.server


def _file_request(name, **arguments):
    request = dispatch_fixtures.request(name)
    request.params['arguments'].pop('params')
    request.params['arguments'].update(arguments)
    return request


def _components(value):
    if isinstance(value, dict):
        if 'type' in value:
            yield value
        for child in value.values():
            yield from _components(child)
    elif isinstance(value, list):
        for child in value:
            yield from _components(child)


def _scalars(value):
    if isinstance(value, dict):
        for child in value.values():
            yield from _scalars(child)
    elif isinstance(value, list):
        for child in value:
            yield from _scalars(child)
    else:
        yield value


def _success(server, request):
    reply = server.process_request(request)
    reply.validate_result_shape()
    assert reply.error is None
    assert reply.request_id == request.request_id
    return reply


class _RenderedHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


def test_mcp_decodes_typed_display_without_replacing_sanitized_result(server, monkeypatch):
    records = [{'name': 'sitea', 'up': True, 'count': 3, 'ratio': 0.5},
               {'name': 'siteb', 'up': False, 'count': 2, 'ratio': 0.25}]
    encoded_records = json.dumps(json.dumps(records))
    encoded_status = json.dumps(json.dumps({'leader_serving': False, 'term': 7}))
    payload = {'status': '10', 'data': encoded_records, 'core_status': encoded_status,
               'service_key': 'fabric-secret'}
    calls = []

    def execute(core, action, params, config, credentials):
        calls.append((action, deepcopy(params)))
        return deepcopy(payload)

    monkeypatch.setattr(client, 'execute', execute)
    reply = _success(server, dispatch_fixtures.request('gaiakeep_core_status'))
    assert calls == [('core.status', {})]
    assert reply.result == {'verdict': 'ok', 'result': {
        'status': '10', 'data': encoded_records, 'core_status': encoded_status}}
    assert payload['service_key'] == 'fabric-secret'
    nodes = list(_components(reply.ui_components))
    assert not any(node['type'] in {'code', 'code_block'} for node in nodes)
    table = next(node for node in nodes if node['type'] == 'table')
    assert len(table['rows']) == 2
    up, count, ratio = (table['headers'].index(label) for label in ('Up', 'Count', 'Ratio'))
    assert table['rows'][0][up] is True and table['rows'][1][up] is False
    assert type(table['rows'][0][count]) is int and table['rows'][0][count] == 3
    assert type(table['rows'][0][ratio]) is float and table['rows'][0][ratio] == 0.5
    assert any(node['type'] == 'badge' and node['label'] == 'Leader not serving'
               and node['variant'] == 'warning' for node in nodes)


def test_mcp_pending_publication_retains_job_and_approved_request_identity(server, monkeypatch):
    payload = {'publication': {'vid': 'version-42', 'commit_job': 'durable-42'}}
    calls = []

    def upload(*arguments):
        calls.append(arguments)
        return deepcopy(payload)

    monkeypatch.setattr(client, 'upload', upload)
    request = _file_request('gaiakeep_upload_file', collection_id='runs', path='metrics.json',
                            data_base64='e30=', request_id='approved_request_42', note='completed run')
    reply = _success(server, request)
    assert len(calls) == 1 and calls[0][-2:] == ('approved_request_42', 'completed run')
    assert reply.result == {'verdict': 'pending', 'result': payload, 'reconciliation': {
        'collection_id': 'runs', 'path': 'metrics.json', 'branch': 'main',
        'request_id': 'approved_request_42', 'vid': 'version-42', 'commit_job': 'durable-42'}}
    assert reply.ui_components[0]['title'] == 'GaiaKeep version awaiting durable copies'
    nodes = list(_components(reply.ui_components))
    assert any(node['type'] == 'badge' and node['label'] == 'Awaiting durable copies'
               and node['variant'] == 'warning' for node in nodes)
    assert not any(node.get('variant') == 'success' for node in nodes)
    assert 'durable-42' in list(_scalars(reply.ui_components))


def test_mcp_invalid_pending_job_fails_without_a_presented_success(server, monkeypatch):
    attempts, failures = [], []

    def upload(*arguments):
        attempts.append(arguments[-2])
        return {'publication': {'vid': 'version-42', 'commit_job': '../untrusted-job'}}

    monkeypatch.setattr(client, 'upload', upload)
    monkeypatch.setattr(server.issue_log, 'record',
                        lambda name, verdict, **state: failures.append((name, verdict, state)))
    reply = server.process_request(_file_request(
        'gaiakeep_upload_file', collection_id='runs', path='metrics.json', data_base64='e30=',
        request_id='approved_request_42'))
    reply.validate_result_shape()
    assert attempts == ['approved_request_42']
    assert reply.error['retryable'] is False
    assert reply.error['data']['verdict'] == 'unconfirmed'
    assert reply.error['data']['reconciliation']['request_id'] == 'approved_request_42'
    assert 'commit_job' not in reply.error['data']['reconciliation']
    assert not reply.ui_components
    assert failures == [('gaiakeep_upload_file', 'unconfirmed', {'dispatched': True, 'mutation': True})]


def test_mcp_binary_bytes_remain_in_result_and_are_absent_from_display(server, monkeypatch):
    content = b'archival-binary-display-sentinel\x00\xff'
    encoded = base64.b64encode(content).decode('ascii')
    payload = {'vid': 'version-42', 'path': 'payload.bin', 'byte_count': len(content),
               'sha256': hashlib.sha256(content).hexdigest(), 'data_base64': encoded}
    calls = []

    def read(core, vid, path):
        calls.append((vid, path))
        return deepcopy(payload)

    monkeypatch.setattr(client, 'read', read)
    reply = _success(server, _file_request('gaiakeep_read_file', vid='version-42', path='payload.bin'))
    assert calls == [('version-42', 'payload.bin')]
    assert reply.result == {'verdict': 'ok', 'result': payload}
    assert base64.b64decode(reply.result['result']['data_base64'], validate=True) == content
    display = json.dumps(reply.ui_components)
    assert encoded not in display and 'data_base64' not in display
    assert payload['sha256'] in display and 'payload.bin' in display


def test_mcp_dataset_retains_approved_literal_identifiers_paths_and_note(server, monkeypatch):
    monkeypatch.setenv('GAIAKEEP_CONNECTION_MODE', 'ssh')
    path, note = '["metrics"]', '[{"reviewed":true}]'
    digest = 'a' * 64
    payload = {'collection_id': '42', 'dataset_ref': 'run42', 'vid': '17', 'note': note,
               'manifest_sha256': digest, 'files': [{'path': path, 'size': 1, 'sha256': 'b' * 64}]}
    calls = []
    core = RemoteCore(SimpleNamespace(), {})
    core.perform = lambda *arguments: calls.append(deepcopy(arguments)) or deepcopy(payload)

    @contextmanager
    def opened(*arguments):
        yield core, SimpleNamespace(service_key='', allow_legacy=False)

    monkeypatch.setattr(client, 'open_client', opened)
    arguments = {'collection_id': '42', 'dataset_ref': 'run42', 'manifest_sha256': digest,
                 'request_id': 'approved_request_42', 'prefix': path, 'note': note}
    reply = _success(server, _file_request('gaiakeep_upload_dataset', **arguments))
    forwarded = {key: value for key, value in arguments.items() if key != 'request_id'}
    assert calls == [('upload_dataset', forwarded, 'approved_request_42')]
    assert reply.result['result'] == payload
    assert reply.result['reconciliation'] == {
        'collection_id': '42', 'dataset_ref': 'run42', 'manifest_sha256': digest,
        'branch': 'main', 'request_id': 'approved_request_42'}
    values = list(_scalars(reply.ui_components))
    assert all(literal in values for literal in ('42', '17', path, note, digest))


def test_mcp_display_suppresses_secret_fields_and_decoded_known_secrets(server, monkeypatch):
    decoded = {'visible': 'displayable-observation', 'password': 'ui-only-password-marker',
               'api_token': 'ui-only-token-marker', 'credentials': 'ui-only-credential-marker',
               'private_key': 'ui-only-key-marker',
               'observations': [{'name': 'native', 'value': 'fabric-secret ssh-password credential-sentinel'}]}
    escaped_inner = json.dumps(decoded).replace('fabric-secret', r'\u0066abric-secret')
    encoded = json.dumps(escaped_inner)
    payload = {'status': '10', 'data': encoded, 'service_key': 'fabric-secret'}
    monkeypatch.setattr(client, 'execute', lambda *arguments: deepcopy(payload))
    request = dispatch_fixtures.request('gaiakeep_core_status')
    request.params['arguments']['_credentials'] = {'GAIAKEEP_PRIVATE_KEY': 'credential-sentinel'}
    reply = _success(server, request)
    canonical = reply.result['result']
    assert canonical == {'status': '10', 'data': encoded.replace('ssh-password', '[redacted]').replace(
        'credential-sentinel', '[redacted]')}
    assert 'ui-only-password-marker' in canonical['data']
    display = json.dumps(reply.ui_components)
    assert 'displayable-observation' in display
    assert all(secret not in display for secret in (
        'fabric-secret', 'ssh-password', 'credential-sentinel', 'ui-only-password-marker',
        'ui-only-token-marker', 'ui-only-credential-marker', 'ui-only-key-marker'))
    assert all(field not in display for field in ('private_key', 'api_token', 'credentials', 'Password'))


def test_mcp_hostile_decoded_text_is_escaped_by_the_actual_renderer(server, monkeypatch):
    from webrender import render

    image = '<img src=x onerror=alert(1)>'
    script = '<script>alert(2)</script>'
    encoded = json.dumps(json.dumps([{'name': image, 'description': script}]))
    monkeypatch.setattr(client, 'execute', lambda *arguments: {'status': '10', 'data': encoded})
    reply = _success(server, dispatch_fixtures.request('gaiakeep_core_status'))
    assert reply.result['result']['data'] == encoded
    values = list(_scalars(reply.ui_components))
    assert image in values and script in values
    html = render(reply.ui_components)
    parsed = _RenderedHTML()
    parsed.feed(html)
    assert image in ''.join(parsed.text) and script in ''.join(parsed.text)
    assert not any(tag in {'img', 'script'} or 'onerror' in attrs for tag, attrs in parsed.elements)
    assert image not in html and script not in html
