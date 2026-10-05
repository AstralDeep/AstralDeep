"""Exercises bounded native framing, exact correlation and TLS configuration without live fabric dependencies."""

import base64
import gzip
import json
import ssl
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from agents.gaiakeep import transport


class Socket:
    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.sent = []
        self.closed = False

    def send(self, data):
        self.sent.append(data)

    def recv(self, timeout=None):
        item = self.replies.pop(0)
        return item(self) if callable(item) else item

    def close(self):
        self.closed = True


@pytest.fixture
def fabric():
    config = SimpleNamespace(core_address='r:a:p', allowed_peers=frozenset({'r:a:p'}),
                             service_key='never-show-key', tls_name='gateway', port=8282)
    return transport.NativeTransport(None, config, ssl.create_default_context())


def wire_reply(ws, **fields):
    sent = json.loads(ws.sent[-1])
    return json.dumps(dict(status='10', client_rpc_id=sent['message_payload']['client_rpc_id'], **fields))


def install_socket(monkeypatch, fabric, ws):
    @contextmanager
    def opened(path):
        assert path == '/api/apisocket'
        try:
            yield ws
        finally:
            ws.close()
    monkeypatch.setattr(fabric, '_socket', opened)


def test_rpc(monkeypatch, fabric):
    ws = Socket([lambda w: wire_reply(w, value='actual')])
    install_socket(monkeypatch, fabric, ws)
    assert fabric.call('r:a:p', 'core.whoami', {'principal': 'owner'}, 20)['value'] == 'actual'
    sent = json.loads(ws.sent[0])
    assert sent['message_info']['dst_plugin'] == 'p'
    assert sent['message_info']['rpc_timeout_ms'] == '19000'
    assert ws.closed


def test_native_publication_retains_commit_job_even_when_ingest_result_drops_it(monkeypatch, fabric):
    from agents.gaiakeep import client

    ws = Socket([lambda w: wire_reply(w, vid='version-42', commit_job='durable-42', job_id='ingest-42')])
    install_socket(monkeypatch, fabric, ws)
    fabric.call('r:a:p', 'core.publish', {'request_id': 'retained-request', 'upload_id': 'upload-42'}, 20)
    core = SimpleNamespace(transport=fabric)
    result = client.publication_result(core, SimpleNamespace(vid='version-42', request_id='retained-request'))
    assert result['commit_job'] == 'durable-42' and result['job_id'] == 'ingest-42'
    assert len(ws.sent) == 1


@pytest.mark.parametrize('job', [None, [], {}, 1, '', '../private-job', 'x' * 257])
def test_native_malformed_publication_job_never_reports_success_or_replays(monkeypatch, fabric, job):
    from agents.gaiakeep import client

    ws = Socket([lambda w: wire_reply(w, vid='version-42', commit_job=job)])
    install_socket(monkeypatch, fabric, ws)
    with pytest.raises(client.AgentError) as error:
        fabric.call('r:a:p', 'core.publish', {'request_id': 'retained-request'}, 20)
    assert error.value.verdict == 'unconfirmed' and len(ws.sent) == 1 and not fabric.publication_metadata


def test_native_committing_read_preserves_safe_reconciliation_without_republication(monkeypatch, fabric):
    from agents.gaiakeep import client

    def pending(w):
        sent = json.loads(w.sent[-1])
        return json.dumps({'status': '16', 'client_rpc_id': sent['message_payload']['client_rpc_id'],
                           'commit_job': 'durable-42', 'vid': 'version-42', 'secret': 'private-value'})
    ws = Socket([pending])
    install_socket(monkeypatch, fabric, ws)
    with pytest.raises(client.AgentError) as error:
        fabric.call('r:a:p', 'core.list', {'vid': 'version-42'}, 20)
    assert error.value.verdict == 'pending'
    assert error.value.reconciliation == {'commit_job': 'durable-42', 'vid': 'version-42'}
    assert transport.failure_detail(error.value)['native_status'] == 16 and len(ws.sent) == 1


@pytest.mark.parametrize('reply', ['[]', '{', json.dumps({'status': '10', 'client_rpc_id': 'wrong'})])
def test_bad_correlation(monkeypatch, fabric, reply):
    ws = Socket([reply])
    install_socket(monkeypatch, fabric, ws)
    with pytest.raises(transport.ProtocolError):
        fabric.call('r:a:p', 'core.whoami', {}, 20)
    assert ws.closed


def test_route_refused(fabric):
    with pytest.raises(transport.ProtocolError):
        fabric.call('foreign:a:p', 'core.whoami', {}, 20)


def test_compressed_bounds():
    value = base64.b64encode(gzip.compress(b'{"files":[]}')).decode()
    assert transport.decode_compressed(value) == {'files': []}
    with pytest.raises(transport.ProtocolError):
        transport.decode_compressed(base64.b64encode(gzip.compress(b'x' * (transport.MAX_EXPANDED + 1))).decode())


@pytest.mark.parametrize('value', ['invalid', base64.b64encode(b'not-gzip').decode()])
def test_bad_compression(value):
    with pytest.raises(transport.ProtocolError):
        transport.decode_compressed(value)


def test_config_is_required_and_validated(monkeypatch, tmp_path):
    names = ['GAIAKEEP_CORE_ADDRESS', 'GAIAKEEP_ALLOWED_PEERS', 'GAIAKEEP_GATEWAY_TLS_NAME',
             'GAIAKEEP_GATEWAY_CA_FILE', 'GAIAKEEP_CORE_PUBLIC_KEY', 'CRESCO_SERVICE_KEY']
    for name in names:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError):
        transport.GatewayConfig.from_environment()
    for name, value in zip(names, ['r:a:p', 'r:a:p,r:a:q', 'gateway.test', '/trusted/ca.pem', 'key-pin', 'runtime-key']):
        monkeypatch.setenv(name, value)
    config = transport.GatewayConfig.from_environment()
    assert config.allowed_peers == {'r:a:p', 'r:a:q'}
    assert 'runtime-key' not in repr(config)
    monkeypatch.setenv('GAIAKEEP_CORE_ADDRESS', 'foreign:a:p')
    with pytest.raises(ValueError):
        transport.GatewayConfig.from_environment()
    monkeypatch.setenv('GAIAKEEP_CORE_ADDRESS', 'r:a:p')
    monkeypatch.setenv('GAIAKEEP_ALLOWED_PEERS', 'r:a:p,invalid')
    with pytest.raises(ValueError):
        transport.GatewayConfig.from_environment()
    monkeypatch.setenv('GAIAKEEP_ALLOWED_PEERS', 'r:a:p')
    monkeypatch.setenv('GAIAKEEP_GATEWAY_PORT', '70000')
    with pytest.raises(ValueError):
        transport.GatewayConfig.from_environment()


def test_verification_cannot_be_disabled(fabric):
    context = ssl._create_unverified_context()
    with pytest.raises(ValueError):
        transport.NativeTransport(None, fabric.config, context)


def test_deadline_and_wire_bounds(fabric, monkeypatch):
    with pytest.raises(transport.ProtocolError):
        fabric.call('r:a:p', 'core.status', {'large': 'x' * transport.MAX_RPC}, 20)
    fabric.deadline = 0
    with pytest.raises(TimeoutError):
        fabric.call('r:a:p', 'core.status', {}, 20)
    with pytest.raises(transport.ProtocolError):
        transport._json(b'binary')
    with pytest.raises(transport.ProtocolError):
        transport.decode_compressed(5)
    with pytest.raises(transport.ProtocolError):
        fabric.open_stream('untrusted selector', None)


def test_rpc_compression_is_bounded_before_sdk(monkeypatch, fabric):
    encoded = base64.b64encode(gzip.compress(b'{"audience":"a"}')).decode()
    ws = Socket([lambda w: wire_reply(w, core_status=encoded)])
    install_socket(monkeypatch, fabric, ws)
    assert fabric.call('r:a:p', 'core.status', {}, 20)['core_status'] == encoded


@pytest.mark.parametrize('content', ['"' + 'x' * (transport.MAX_EXPANDED // 2 + 1) + '"', 'NaN'],
                         ids=['aggregate', 'nonfinite'])
def test_compressed_aggregate_and_nonfinite_reply(monkeypatch, fabric, content):
    encoded = base64.b64encode(gzip.compress(content.encode())).decode()
    ws = Socket([lambda w: wire_reply(w, first=encoded, second=encoded)])
    install_socket(monkeypatch, fabric, ws)
    with pytest.raises(transport.ProtocolError):
        fabric.call('r:a:p', 'core.status', {}, 20)
    assert ws.closed


@pytest.mark.parametrize('prefix', [' ', '!', '\n', 'H4sI!'])
def test_permissive_sdk_compression_cannot_bypass_native_bounds(monkeypatch, fabric, prefix):
    encoded = base64.b64encode(gzip.compress(b'"' + b'x' * transport.MAX_EXPANDED + b'"')).decode()
    ws = Socket([lambda w: wire_reply(w, core_status=prefix + encoded)])
    install_socket(monkeypatch, fabric, ws)
    with pytest.raises(transport.ProtocolError):
        fabric.call('r:a:p', 'core.status', {}, 20)
    assert ws.closed


def test_every_stream_closes_even_if_one_cleanup_fails(fabric):
    from unittest.mock import MagicMock
    first, second = MagicMock(), MagicMock()
    first.close.side_effect = OSError('cleanup')
    fabric.streams.extend([first, second])
    with pytest.raises(OSError):
        fabric.close()
    second.close.assert_called_once()
    assert fabric.streams == []


def test_failed_websocket_close_still_releases_stream_manager(fabric):
    from unittest.mock import MagicMock
    stream = object.__new__(transport.NativeStream)
    stream.closed, stream.worker = False, None
    stream.ws, stream.manager = MagicMock(), MagicMock()
    stream.ws.close.side_effect = OSError('cleanup')
    with pytest.raises(OSError):
        stream.close()
    stream.manager.__exit__.assert_called_once_with(None, None, None)


@pytest.mark.parametrize('activation', ['{"status_code":"9"}', '[]'])
def test_dataplane_activation_failure_closes(monkeypatch, fabric, activation):
    ws = Socket([activation])
    @contextmanager
    def opened(path):
        assert path == '/api/dataplane'
        try:
            yield ws
        finally:
            ws.close()
    monkeypatch.setattr(fabric, '_socket', opened)
    with pytest.raises(transport.ProtocolError):
        fabric.open_stream('generated-stream', None)
    assert ws.closed


def test_stream_send_bounds_and_failed_receiver(monkeypatch, fabric):
    class Worker:
        def __init__(self, **kwargs):
            pass
        def start(self):
            pass
        def join(self, **kwargs):
            pass
    monkeypatch.setattr(transport.threading, 'Thread', Worker)
    ws = Socket(['{"status_code":"10"}', b'echo', b'x' * (transport.MAX_FRAME + 1)])
    @contextmanager
    def opened(path):
        yield ws
    monkeypatch.setattr(fabric, '_socket', opened)
    stream = fabric.open_stream('generated-stream', None)
    with pytest.raises(transport.ProtocolError):
        stream.send(b'x' * (transport.MAX_FRAME + 1))
    stream.send(b'GKdata')
    stream._receive()
    assert isinstance(stream.error, transport.ProtocolError)
    with pytest.raises(transport.ProtocolError):
        stream.send(b'GKdata')
    fabric.close()
    stream.close()
    assert stream.closed


def test_bridge_idle_timeout_is_not_disconnect():
    import threading
    import time
    class Connection:
        def __init__(self, packets=()):
            self.packets, self.sent, self.closed = list(packets), [], False
        def recv(self, size):
            value = self.packets.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        def sendall(self, value):
            self.sent.append(value)
        def shutdown(self, mode):
            raise OSError()
        def close(self):
            self.closed = True
    source = Connection([TimeoutError(), b'reply-after-idle', b''])
    destination = Connection()
    bridge = object.__new__(transport.SocketBridge)
    bridge.socket, bridge.peer, bridge.channel = source, destination, destination
    bridge.workers, bridge.stopped, bridge.deadline = [], threading.Event(), time.monotonic() + 120
    bridge._pump(source, destination)
    assert destination.sent == [b'reply-after-idle']
    assert source.closed and destination.closed
