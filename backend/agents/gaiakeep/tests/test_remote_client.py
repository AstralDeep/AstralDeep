"""Verifies pinned SSH execution, bounded streams, immutable bootstrap and verified file results."""

import base64
import hashlib
import json
import shlex
import subprocess
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from agents.gaiakeep import client, remote_client as remote


class Channel:
    def __init__(self, result=b'{"ok":true,"result":{}}', stderr=b'', status=0):
        self.result, self.stderr, self.status = result, stderr, status
        self.closed = self.shutdown = False
        self.sent = bytearray()

    def settimeout(self, value):
        self.timeout = value

    def exec_command(self, command):
        self.command = command

    def send_ready(self):
        return True

    def send(self, data):
        self.sent.extend(data[:1000])
        return min(1000, len(data))

    def shutdown_write(self):
        self.shutdown = True

    def recv_ready(self):
        return self.shutdown and bool(self.result)

    def recv(self, size):
        chunk, self.result = self.result[:size], self.result[size:]
        return chunk

    def recv_stderr_ready(self):
        return bool(self.stderr)

    def recv_stderr(self, size):
        chunk, self.stderr = self.stderr[:size], self.stderr[size:]
        return chunk

    def exit_status_ready(self):
        return self.shutdown

    def recv_exit_status(self):
        return self.status

    def close(self):
        self.closed = True


@pytest.fixture
def connected(monkeypatch):
    channel = Channel()
    connection = SimpleNamespace(closed=False)
    connection.close = lambda: setattr(connection, 'closed', True)
    connection.get_transport = lambda: SimpleNamespace(open_session=lambda **kw: channel)
    monkeypatch.setattr(remote.ParamikoTransport, '_connect', lambda *a: (connection, None))
    monkeypatch.setattr(remote.time, 'sleep', lambda seconds: None)
    return channel, connection, SimpleNamespace(host_key_fingerprint='SHA256:pinned')


def test_bounded_input_is_not_in_command_and_all_connections_close(connected):
    channel, connection, target = connected
    bundle = {'remote_runtime.py': 'public source'}
    request = {'arguments': {'data_base64': 'private-file-value'}}
    assert remote._exchange(target, bundle, request).startswith(b'{')
    assert json.loads(channel.sent)['arguments'] == request['arguments']
    assert 'private-file-value' not in channel.command
    assert channel.closed and connection.closed


def test_pin_and_input_bound_refuse_before_connection(monkeypatch):
    monkeypatch.setattr(remote.ParamikoTransport, '_connect', lambda *a: pytest.fail('connection attempted'))
    with pytest.raises(remote.HostKeyMismatch):
        remote._exchange(SimpleNamespace(host_key_fingerprint=None), {}, {})
    monkeypatch.setattr(remote, 'MAX_WIRE', 20)
    with pytest.raises(remote.ProtocolError):
        remote._exchange(SimpleNamespace(host_key_fingerprint='pin'), {}, {'value': 'x' * 50})


@pytest.mark.parametrize('failure', ['stdout', 'stderr', 'exit', 'send', 'timeout', 'exec', 'open'])
def test_disconnect_bounds_and_cleanup(connected, monkeypatch, failure):
    channel, connection, target = connected
    if failure == 'stdout':
        channel.result = b'x' * 100
        monkeypatch.setattr(remote, 'MAX_WIRE', 30)
    elif failure == 'stderr':
        channel.stderr = b'secret' * 4000
    elif failure == 'exit':
        channel.status = 1
    elif failure == 'send':
        channel.send = lambda data: 0
    elif failure == 'timeout':
        channel.exit_status_ready = lambda: False
        ticks = iter([0, 1, 2, 3, 200])
        monkeypatch.setattr(remote.time, 'monotonic', lambda: next(ticks, 200))
    elif failure == 'exec':
        channel.exec_command = lambda command: (_ for _ in ()).throw(OSError('closed'))
    else:
        connection.get_transport = lambda: SimpleNamespace(
            open_session=lambda **kw: (_ for _ in ()).throw(OSError('closed')))
    with pytest.raises((remote.ProtocolError, TimeoutError, OSError, EOFError)):
        remote._exchange(target, {}, {})
    assert connection.closed
    if failure != 'open':
        assert channel.closed


def test_temporary_send_backpressure_and_stderr_are_drained(connected):
    channel, connection, target = connected
    send = channel.send
    calls = []
    def blocked(data):
        if not calls:
            calls.append(1)
            raise TimeoutError
        return send(data)
    channel.send = blocked
    channel.stderr = b'private diagnostic'
    assert remote._exchange(target, {}, {}) == b'{"ok":true,"result":{}}'
    assert not channel.stderr
    assert connection.closed


def test_bootstrap_compiles_checked_sources_and_rejects_corruption():
    bundle = {'remote_runtime.py': 'def run(request):\n return {"ok":True,"result":{"value":request["value"]}}\n'}
    code = shlex.split(remote._command(bundle))[-1]
    payload = dict(value='stdin-only', bundle=bundle)
    code = code.replace('signal.signal(signal.SIGALRM,expire);signal.alarm(110)', '')
    result = subprocess.run([sys.executable, '-I', '-c', code],
                            input=json.dumps(payload).encode(), capture_output=True, timeout=10)
    assert result.returncode == 0
    assert json.loads(result.stdout)['result']['value'] == 'stdin-only'
    payload['bundle']['remote_runtime.py'] = 'raise RuntimeError("corrupt")'
    result = subprocess.run([sys.executable, '-I', '-c', code],
                            input=json.dumps(payload).encode(), capture_output=True, timeout=10)
    assert result.returncode != 0 and not result.stdout


def test_bootstrap_deadline_unwinds_uploaded_bytes_and_bundle(tmp_path):
    marker = tmp_path / 'owned-directories.json'
    source = ('import __main__,json,tempfile\nfrom pathlib import Path\n'
              'def run(request):\n'
              ' with tempfile.TemporaryDirectory(prefix="astral-gaia-upload-") as upload:\n'
              '  Path(upload,"payload").write_bytes(b"private upload")\n'
              f'  Path({str(marker)!r}).write_text(json.dumps([upload,str(Path(__file__).parents[2])]))\n'
              '  __main__.expire()\n')
    bundle = {'remote_runtime.py': source}
    code = shlex.split(remote._command(bundle))[-1]
    code = code.replace('signal.signal(signal.SIGALRM,expire);signal.alarm(110)', '')
    result = subprocess.run([sys.executable, '-I', '-c', code], input=json.dumps({'bundle': bundle}).encode(),
                            capture_output=True, timeout=10)
    assert result.returncode != 0 and not result.stdout
    from pathlib import Path
    assert all(not Path(path).exists() for path in json.loads(marker.read_text()))


@pytest.fixture
def trusted(monkeypatch, tmp_path):
    path = tmp_path / 'ca.pem'
    path.write_text('operator certificate')
    values = {'GAIAKEEP_CORE_ADDRESS': 'r:a:p', 'GAIAKEEP_ALLOWED_PEERS': 'r:a:p,r:b:p',
              'GAIAKEEP_GATEWAY_TLS_NAME': 'gateway.example', 'GAIAKEEP_GATEWAY_CA_FILE': str(path),
              'GAIAKEEP_CORE_PUBLIC_KEY': 'pinned-public-key'}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(remote.GatewayConfig, 'tls_context', lambda self: object())
    return path


def test_remote_trust_and_no_exported_gaia_secret(trusted):
    config, trust = remote._trust()
    assert config.port == 28282
    assert config.service_key == ''
    assert trust['ca_pem'] == 'operator certificate'
    assert 'service_key' not in trust and 'private_key' not in trust
    with remote.open_remote_client(SimpleNamespace(host_key_fingerprint='pin')) as (core, opened):
        assert isinstance(core, remote.RemoteCore) and opened == config
    with pytest.raises(remote.HostKeyMismatch):
        with remote.open_remote_client(SimpleNamespace(host_key_fingerprint=None)):
            pytest.fail('opened')


@pytest.mark.parametrize('field,value', [('GAIAKEEP_CORE_ADDRESS', 'foreign:a:p'),
    ('GAIAKEEP_ALLOWED_PEERS', 'r:a:p,invalid'), ('GAIAKEEP_GATEWAY_TLS_NAME', 'invalid/host'),
    ('GAIAKEEP_GATEWAY_PORT', '0'), ('GAIAKEEP_CORE_PUBLIC_KEY', ''),
    ('GAIAKEEP_GATEWAY_CA_FILE', 'absent-file')])
def test_trust_configuration_failure(trusted, monkeypatch, field, value):
    monkeypatch.setenv(field, value)
    with pytest.raises(client.AgentError) as error:
        remote._trust()
    assert error.value.verdict == 'not_configured'


def test_missing_and_oversized_ca(trusted, monkeypatch):
    trusted.write_text('x' * 65537)
    with pytest.raises(client.AgentError):
        remote._trust()
    monkeypatch.delenv('GAIAKEEP_GATEWAY_CA_FILE')
    with pytest.raises(client.AgentError):
        remote._trust()


def test_bundle_contains_only_reviewed_files():
    assert set(remote._bundle()) == set(remote.BUNDLE_FILES)


def remote_reply(monkeypatch, reply):
    monkeypatch.setattr(remote, '_bundle', lambda: {})
    monkeypatch.setattr(remote, '_exchange', lambda *a: json.dumps(reply).encode())
    return remote.RemoteCore(SimpleNamespace(), {})


def test_native_and_upload_request_binding(monkeypatch):
    seen = []
    core = remote_reply(monkeypatch, {'ok': True, 'result': {'status': '10'}})
    monkeypatch.setattr(remote, '_exchange', lambda target, bundle, request:
                        seen.append(request) or b'{"ok":true,"result":{"status":"10"}}')
    core.perform('core.repair', {'params': {'request_id': 'stable-request-id'}})
    assert seen[-1]['arguments']['params']['request_id'] == 'stable-request-id'
    core.perform('upload', {'collection_id': 'c', 'path': 'p', 'data_base64': 'aA=='}, 'reconcile-request')
    assert seen[-1]['request_id'] == 'reconcile-request'
    core.perform('upload_dataset', {'collection_id': 'c', 'dataset_ref': 'run42', 'manifest_sha256': 'a' * 64},
                 'reconcile-request')
    assert seen[-1]['tool'] == 'gaiakeep_upload_dataset' and seen[-1]['request_id'] == 'reconcile-request'


@pytest.mark.parametrize('reply', [[], {}, {'ok': 1}, {'ok': False, 'verdict': 'fake'},
                                  {'ok': True}, {'ok': True, 'result': []}])
def test_malformed_result(reply, monkeypatch):
    with pytest.raises(remote.ProtocolError):
        remote_reply(monkeypatch, reply).perform('core.whoami', {'params': {}})


@pytest.mark.parametrize('verdict', ['not_configured', 'auth_failed', 'integrity_error', 'upstream_denied',
    'invalid_argument', 'protocol_error', 'unsupported', 'unconfirmed', 'unavailable', 'pending'])
def test_failure_envelope_never_echoes_remote_message(verdict, monkeypatch):
    core = remote_reply(monkeypatch, {'ok': False, 'verdict': verdict, 'message': 'secret traceback'})
    with pytest.raises(client.AgentError) as error:
        core.perform('core.whoami', {'params': {}})
    assert error.value.verdict == verdict and 'secret' not in str(error.value)


def test_pending_envelope_retains_only_closed_native_reconciliation(monkeypatch):
    core = remote_reply(monkeypatch, {'ok': False, 'verdict': 'pending', 'message': 'private-data',
        'reconciliation': {'commit_job': 'durable-42', 'vid': 'version-42', 'job_id': '../private', 'path': '/private'}})
    with pytest.raises(client.AgentError) as error:
        core.perform('core.list', {'params': {'vid': 'version-42'}})
    assert error.value.reconciliation == {'commit_job': 'durable-42', 'vid': 'version-42'}
    assert 'private' not in str(error.value)


def test_rpc_result_bound(monkeypatch):
    monkeypatch.setattr(remote, 'MAX_RPC', 10)
    with pytest.raises(remote.ProtocolError):
        remote_reply(monkeypatch, {'ok': True, 'result': {}}).perform('core.whoami', {'params': {}})


def test_verified_read_and_corrupt_metadata(monkeypatch):
    data = b'versioned bytes'
    result = {'vid': 'v', 'path': 'p', 'data_base64': base64.b64encode(data).decode(),
              'byte_count': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    assert remote_reply(monkeypatch, {'ok': True, 'result': result}).perform('read', {'vid': 'v', 'path': 'p'}) == result
    for change in ({'sha256': 'bad'}, {'byte_count': True}, {'vid': 'foreign'}, {'path': 'foreign'},
                   {'data_base64': 'invalid!'}):
        with pytest.raises((client.AgentError, remote.ProtocolError)):
            remote_reply(monkeypatch, {'ok': True, 'result': dict(result, **change)}).perform('read', {'vid': 'v', 'path': 'p'})


def test_open_client_mode_and_no_backend_key(monkeypatch):
    seen = []
    @contextmanager
    def opened(target):
        seen.append(target)
        yield 'remote', 'trust'
    monkeypatch.setattr(remote, 'open_remote_client', opened)
    monkeypatch.delenv('GAIAKEEP_CONNECTION_MODE', raising=False)
    with client.open_client('owner-target', {}) as value:
        assert value == ('remote', 'trust')
    assert seen == ['owner-target']
    monkeypatch.setenv('GAIAKEEP_CONNECTION_MODE', 'unknown')
    with pytest.raises(client.AgentError):
        with client.open_client('target', {}):
            pytest.fail('opened')
