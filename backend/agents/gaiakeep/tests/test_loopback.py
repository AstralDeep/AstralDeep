"""Verifies the standalone loopback transport keeps bounded native framing and verified TLS without backend imports."""

import datetime
import json
import queue
import socket
import ssl
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from agents.gaiakeep import transport
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from websockets.sync import client
from websockets.sync.server import serve


@pytest.fixture
def config():
    return transport.GatewayConfig('r:a:p', frozenset({'r:a:p'}), 'gateway.test',
                                   '/unused/ca.pem', 'core-pin', 'runtime-key', 28282)


@pytest.mark.parametrize('port', [0, 65536, -1, '28282', True, 28282.0])
def test_invalid_operator_port_is_refused(config, port):
    with pytest.raises(ValueError, match='valid operator port'):
        transport.VerifiedLoopbackTransport(replace(config, port=port), ssl.create_default_context())


@pytest.mark.parametrize('port', [1, 443, 8282, 28282, 65535])
def test_valid_operator_port_is_admitted(config, port):
    native = transport.VerifiedLoopbackTransport(replace(config, port=port), ssl.create_default_context())
    assert native.port == port


@pytest.mark.parametrize('verify_mode', [ssl.CERT_NONE, ssl.CERT_OPTIONAL, ssl.CERT_REQUIRED])
def test_unverified_context_is_refused(config, verify_mode):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = verify_mode
    with pytest.raises(ValueError, match='Verified TLS'):
        transport.VerifiedLoopbackTransport(config, context)


def test_import_and_construction_need_no_backend_packages():
    source = """
import importlib.abc
import importlib.util
import ssl
import sys
class BlockBackend(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'orchestrator', 'shared'}:
            raise ModuleNotFoundError('backend import refused')
sys.meta_path.insert(0, BlockBackend())
spec = importlib.util.spec_from_file_location('standalone_gateway', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
config = module.GatewayConfig('r:a:p', frozenset({'r:a:p'}), 'gateway.test', '', 'pin', 'key', 28282)
gateway = module.VerifiedLoopbackTransport(config, ssl.create_default_context())
assert gateway.target is None
assert gateway.context.check_hostname
assert not any(name.split('.')[0] in {'orchestrator', 'shared'} for name in sys.modules)
gateway.close()
"""
    result = subprocess.run([sys.executable, '-I', '-c', source, str(Path(transport.__file__).resolve())],
                            capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr


def test_unknown_module_attribute_is_not_synthesized():
    with pytest.raises(AttributeError):
        getattr(transport, 'unrecognized_backend_attribute')


@pytest.fixture
def connections(monkeypatch, config):
    monkeypatch.setattr(transport.time, 'monotonic', lambda: 100)
    native = transport.VerifiedLoopbackTransport(config, ssl.create_default_context())
    raw, ws = MagicMock(), MagicMock()
    dial, upgrade = MagicMock(return_value=raw), MagicMock(return_value=ws)
    monkeypatch.setattr(transport.socket, 'create_connection', dial)
    monkeypatch.setattr(client, 'connect', upgrade)
    return native, raw, ws, dial, upgrade


@pytest.mark.parametrize('path', ['/api/apisocket', '/api/dataplane'])
def test_control_and_data_use_same_fixed_verified_connection(connections, path):
    native, raw, ws, dial, upgrade = connections
    with native._socket(path) as opened:
        assert opened is ws
    dial.assert_called_once_with(('127.0.0.1', 28282), timeout=transport.CONNECT_TIMEOUT)
    upgrade.assert_called_once_with(
        f'wss://gateway.test:28282{path}', sock=raw, ssl=native.context,
        server_hostname='gateway.test', proxy=None,
        additional_headers={'cresco_service_key': 'runtime-key'}, compression=None,
        max_size=transport.MAX_FRAME, max_queue=8,
        open_timeout=transport.CONNECT_TIMEOUT, close_timeout=1,
    )
    ws.close.assert_called_once()
    raw.close.assert_called_once()


def test_expired_operation_dials_nothing(connections):
    native, _, _, dial, upgrade = connections
    native.deadline = 99
    with pytest.raises(TimeoutError):
        with native._socket('/api/apisocket'):
            pytest.fail('expired connection yielded')
    dial.assert_not_called()
    upgrade.assert_not_called()


@pytest.mark.parametrize('change', ['port', 'tls'])
def test_changed_connection_configuration_is_refused_before_dial(connections, change):
    native, _, _, dial, upgrade = connections
    if change == 'port':
        native.config = replace(native.config, port=8282)
    else:
        native.context.check_hostname = False
    with pytest.raises(ValueError):
        with native._socket('/api/apisocket'):
            pytest.fail('invalid connection yielded')
    dial.assert_not_called()
    upgrade.assert_not_called()


@pytest.mark.parametrize('stage', ['dial', 'upgrade', 'body', 'close'])
def test_connections_are_released_after_failures(connections, stage):
    native, raw, ws, dial, upgrade = connections
    if stage == 'dial':
        dial.side_effect = OSError('unavailable')
    elif stage == 'upgrade':
        upgrade.side_effect = ssl.SSLCertVerificationError('untrusted')
    elif stage == 'close':
        ws.close.side_effect = OSError('close failed')
    with pytest.raises(OSError):
        with native._socket('/api/apisocket'):
            if stage == 'body':
                raise OSError('interrupted')
    if stage == 'dial':
        upgrade.assert_not_called()
        raw.close.assert_not_called()
    else:
        raw.close.assert_called_once()


@pytest.fixture
def loopback_tls(tmp_path, monkeypatch):
    key = ec.generate_private_key(ec.SECP384R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'gateway.test')])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(days=1))
                   .not_valid_after(now + datetime.timedelta(days=1))
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName('gateway.test')]), critical=False)
                   .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                   .sign(key, hashes.SHA384()))
    pem, private = tmp_path / 'ca.pem', tmp_path / 'key.pem'
    pem.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(pem, private)
    captured, returned = [], queue.Queue()

    def handler(ws):
        try:
            captured.append((ws.request.path, ws.request.headers['cresco_service_key']))
            request = json.loads(ws.recv(timeout=5))
            if ws.request.path == '/api/apisocket':
                ws.send(json.dumps({'status': '10', 'client_rpc_id': request['message_payload']['client_rpc_id'],
                                    'value': 'verified'}))
            else:
                assert request['ident_id'] == 'bounded-stream'
                ws.send(json.dumps({'status_code': '10'}))
                ws.send(b'verified-inbound')
                returned.put(ws.recv(timeout=5))
        finally:
            ws.close()

    listener = serve(handler, '127.0.0.1', 0, ssl=context, compression=None)
    worker = threading.Thread(target=listener.serve_forever, daemon=True)
    worker.start()
    original = socket.create_connection
    fixture_port = listener.socket.getsockname()[1]
    sockets = []

    def dial(address, timeout):
        assert address == ('127.0.0.1', 28282)
        opened = original(('127.0.0.1', fixture_port), timeout=timeout)
        sockets.append(opened)
        return opened

    monkeypatch.setattr(transport.socket, 'create_connection', dial)
    native = transport.VerifiedLoopbackTransport(
        transport.GatewayConfig('r:a:p', frozenset({'r:a:p'}), 'gateway.test',
                                str(pem), 'core-pin', 'runtime-key', 28282))
    try:
        yield native, captured, returned, sockets
    finally:
        native.close()
        listener.shutdown()
        worker.join(timeout=5)


def test_inherited_rpc_and_streams_are_verified(loopback_tls):
    native, captured, returned, sockets = loopback_tls
    assert native.call('r:a:p', 'core.status', {}, 20)['value'] == 'verified'
    received = queue.Queue()
    stream = native.open_stream('bounded-stream', received.put)
    assert received.get(timeout=5) == b'verified-inbound'
    stream.send(b'verified-outbound')
    assert returned.get(timeout=5) == b'verified-outbound'
    native.close()
    assert captured == [('/api/apisocket', 'runtime-key'), ('/api/dataplane', 'runtime-key')]
    assert all(connection.fileno() == -1 for connection in sockets)


@pytest.mark.parametrize('channel', ['rpc', 'stream'])
@pytest.mark.parametrize('trust', ['ca', 'hostname'])
def test_untrusted_tls_is_refused_on_both_channels(loopback_tls, channel, trust):
    native, captured, _, sockets = loopback_tls
    if trust == 'ca':
        native.context = ssl.create_default_context()
    else:
        native.config = replace(native.config, tls_name='wrong.test')
    with pytest.raises(ssl.SSLCertVerificationError):
        if channel == 'rpc':
            native.call('r:a:p', 'core.status', {}, 20)
        else:
            native.open_stream('bounded-stream', None)
    assert not captured
    assert all(connection.fileno() == -1 for connection in sockets)
