"""Exercises native failure attribution through the real transport, remote adapter and MCP boundaries. Diagnostics remain private operational metadata while primary outcomes, cleanup and single dispatch stay unchanged."""

from contextlib import contextmanager
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from agents.gaiakeep import client, remote_runtime, transport
from agents.gaiakeep.tests import test_dispatch, test_loopback, test_remote_runtime, test_transport
from agents.gaiakeep.tests.test_dispatch import request
from agents.gaiakeep.tests.test_remote_client import remote_reply
from agents.gaiakeep.tests.test_transport import Socket, install_socket

server = test_dispatch.server
config, connections = test_loopback.config, test_loopback.connections
runtime = test_remote_runtime.runtime
fabric = test_transport.fabric

DETAIL = {'native_phase': 'rpc_receive', 'failure_kind': 'timeout', 'native_status': 504}
SECRET = 'private-user-password /patient/path and content'


@pytest.mark.parametrize('phase', ['tcp_connect', 'websocket_open'])
def test_connection_entry_wrapper_retains_original_boundary(fabric, monkeypatch, phase):
    inner = transport.mark_failure(TimeoutError(SECRET), phase)
    openings = []

    @contextmanager
    def opened(path):
        openings.append(path)
        transport._raise_opening_error(inner)
        yield

    monkeypatch.setattr(fabric, '_socket', opened)
    with pytest.raises(transport.ConnectionOpenError) as caught:
        fabric.call('r:a:p', 'core.status', {}, 20)
    assert openings == ['/api/apisocket', '/api/apisocket']
    assert caught.value.__cause__ is inner
    assert transport.failure_detail(caught.value) == {
        'native_phase': phase, 'failure_kind': 'timeout', 'native_status': None}


@pytest.mark.parametrize('stage', ['send', 'receive', 'decode'])
def test_rpc_failure_keeps_identity_and_attribution(fabric, monkeypatch, stage):
    ws = MagicMock()
    error = TimeoutError(SECRET)
    if stage == 'send':
        ws.send.side_effect = error
    elif stage == 'receive':
        ws.recv.side_effect = error
    else:
        ws.recv.return_value = '[]'
    install_socket(monkeypatch, fabric, ws)
    with pytest.raises((TimeoutError, transport.ProtocolError)) as caught:
        fabric.call('r:a:p', 'core.status', {}, 20)
    phase = {'send': 'rpc_send', 'receive': 'rpc_receive', 'decode': 'rpc_decode'}[stage]
    assert transport.failure_detail(caught.value)['native_phase'] == phase
    if stage != 'decode':
        assert caught.value is error
    ws.send.assert_called_once()
    ws.close.assert_called_once()


def test_correlation_failure_records_only_numeric_status(fabric, monkeypatch):
    ws = Socket([json.dumps({'client_rpc_id': 'different', 'status': '504', 'secret': SECRET})])
    install_socket(monkeypatch, fabric, ws)
    with pytest.raises(transport.ProtocolError) as caught:
        fabric.call('r:a:p', 'core.status', {}, 20)
    assert transport.failure_detail(caught.value) == {
        'native_phase': 'rpc_response', 'failure_kind': 'protocol', 'native_status': 504}
    assert SECRET not in json.dumps(transport.failure_detail(caught.value))


def test_raw_connection_cleanup_is_tagged_and_not_retried(connections):
    native, raw, ws, dial, upgrade = connections
    error = OSError(SECRET)
    raw.close.side_effect = error
    with pytest.raises(OSError) as caught:
        with native._socket('/api/apisocket'):
            pass
    assert caught.value is error
    assert transport.failure_detail(error)['native_phase'] == 'cleanup'
    dial.assert_called_once()
    upgrade.assert_called_once()
    raw.close.assert_called_once()
    ws.close.assert_called_once()


@pytest.mark.parametrize('stage', ['upgrade', 'websocket_close', 'bridge_close'])
def test_native_tunnel_boundary_preserves_original_exception(fabric, monkeypatch, stage):
    from websockets.sync import client as websocket_client

    fabric.target = SimpleNamespace(address='registered.machine')
    channel, bridge, ws = MagicMock(), MagicMock(), MagicMock()
    error = OSError(SECRET)

    @contextmanager
    def tunnel(self, target, port, timeout):
        assert target is fabric.target and port == fabric.config.port and timeout == transport.CONNECT_TIMEOUT
        yield channel

    monkeypatch.setattr(transport.ParamikoTransport, 'open_tunnel', tunnel)
    monkeypatch.setattr(transport, 'validate_egress_url', lambda url: None)
    monkeypatch.setattr(transport, 'SocketBridge', lambda *args: bridge)
    upgrade = MagicMock(return_value=ws)
    monkeypatch.setattr(websocket_client, 'connect', upgrade)
    if stage == 'upgrade':
        upgrade.side_effect = error
    elif stage == 'websocket_close':
        ws.close.side_effect = error
    else:
        bridge.close.side_effect = error
    with pytest.raises(OSError) as caught:
        with fabric._socket('/api/apisocket'):
            pass
    assert caught.value is error
    assert transport.failure_detail(error)['native_phase'] == ('websocket_open' if stage == 'upgrade' else 'cleanup')
    upgrade.assert_called_once()
    bridge.close.assert_called_once()


@pytest.mark.parametrize('stage', ['open', 'activation_send', 'activation_receive'])
def test_stream_start_failure_retains_boundary_without_worker(fabric, monkeypatch, stage):
    ws = MagicMock()
    error = TimeoutError(SECRET)

    @contextmanager
    def opened(path):
        if stage == 'open':
            raise error
        yield ws

    monkeypatch.setattr(fabric, '_socket', opened)
    if stage == 'activation_send':
        ws.send.side_effect = error
    elif stage == 'activation_receive':
        ws.recv.side_effect = error
    with pytest.raises(TimeoutError) as caught:
        fabric.open_stream('generated', None)
    assert caught.value is error
    assert transport.failure_detail(error)['native_phase'] == {
        'open': 'stream_open', 'activation_send': 'stream_send',
        'activation_receive': 'stream_activation'}[stage]
    assert fabric.streams == []
    if stage != 'open':
        ws.close.assert_called_once()


@pytest.mark.parametrize('stage', ['deadline', 'send', 'manager_close'])
def test_stream_failure_preserves_exception_and_dispatch_count(fabric, monkeypatch, stage):
    stream = object.__new__(transport.NativeStream)
    stream.closed, stream.worker, stream.error = False, None, None
    stream.transport, stream.ws, stream.manager = fabric, MagicMock(), MagicMock()
    error = TimeoutError(SECRET)
    if stage == 'deadline':
        monkeypatch.setattr(fabric, 'remaining', MagicMock(side_effect=error))
    elif stage == 'send':
        stream.ws.send.side_effect = error
    else:
        stream.manager.__exit__.side_effect = error
    with pytest.raises(TimeoutError) as caught:
        stream.close() if stage == 'manager_close' else stream.send(b'GKbounded')
    assert caught.value is error
    assert transport.failure_detail(error)['native_phase'] == ('cleanup' if stage == 'manager_close' else 'stream_send')
    assert stream.ws.send.call_count == (1 if stage == 'send' else 0)


def test_receive_failure_is_carried_into_existing_protocol_error(fabric):
    stream = object.__new__(transport.NativeStream)
    stream.closed, stream.worker = False, None
    stream.transport, stream.ws = fabric, MagicMock()
    stream.error = transport.mark_failure(ConnectionError(SECRET), 'stream_receive')
    with pytest.raises(transport.ProtocolError) as caught:
        stream.send(b'GKbounded')
    assert transport.failure_detail(caught.value)['native_phase'] == 'stream_receive'
    stream.ws.send.assert_not_called()


@pytest.mark.parametrize('mutation', [False, True])
def test_remote_sdk_failure_retains_verdict_and_validated_detail(runtime, monkeypatch, mutation):
    calls = []
    error = transport.mark_failure(TimeoutError(SECRET), 'rpc_receive', native_status=504)

    def failed(*args, **kwargs):
        calls.append(True)
        raise error

    monkeypatch.setattr(runtime.Core, 'read', failed)
    monkeypatch.setattr(runtime.Core, 'ingest', failed)
    arguments = {'machine_id': 'mine', 'vid': 'v', 'path': 'p'}
    if mutation:
        arguments = {'machine_id': 'mine', 'collection_id': 'c', 'path': 'p',
                     'data_base64': 'aA==', 'request_id': 'a' * 32}
    native_request = runtime.request()
    native_request.update(tool='gaiakeep_upload_file' if mutation else 'gaiakeep_read_file', arguments=arguments)
    if mutation:
        native_request['request_id'] = arguments['request_id']
    out = remote_runtime.run(native_request)
    assert out['ok'] is False and out['verdict'] == ('unconfirmed' if mutation else 'unavailable')
    assert out['failure_detail'] == DETAIL and calls == [True]
    assert SECRET not in json.dumps(out)
    assert runtime.records['closed'] == ['core', 'transport']


@pytest.mark.parametrize('value', [DETAIL, {'native_phase': SECRET}, dict(DETAIL, extra=SECRET)])
def test_remote_core_discards_malformed_detail_without_changing_primary(monkeypatch, value):
    core = remote_reply(monkeypatch, {'ok': False, 'verdict': 'unavailable',
                                    'message': SECRET, 'failure_detail': value})
    with pytest.raises(client.AgentError) as caught:
        core.perform('core.whoami', {'params': {}})
    assert caught.value.verdict == 'unavailable' and SECRET not in str(caught.value)
    assert transport.failure_detail(caught.value) == (DETAIL if value == DETAIL else None)


@pytest.mark.parametrize('mutation', [False, True])
def test_mcp_safe_detail_is_only_operational_and_survives_mutation_mapping(server, monkeypatch, mutation):
    recorded = []
    error = transport.mark_failure(transport.ProtocolError(SECRET), 'rpc_receive', native_status=504)
    monkeypatch.setattr(server.issue_log, 'record', lambda *args, **kwargs: recorded.append((args, kwargs)))

    def failed(*args):
        raise error

    monkeypatch.setattr(client, 'execute', failed)
    name = 'gaiakeep_core_repair' if mutation else 'gaiakeep_core_whoami'
    reply = server.process_request(request(name))
    assert reply.error['data']['verdict'] == ('unconfirmed' if mutation else 'protocol_error')
    assert len(recorded) == 1 and recorded[0][1]['detail'] == dict(DETAIL, failure_kind='protocol')
    assert recorded[0][1]['dispatched'] is True and recorded[0][1]['mutation'] is mutation
    assert 'native_phase' not in json.dumps(reply.error) + json.dumps(reply.ui_components)
    assert SECRET not in str(recorded) + str(reply.error)
