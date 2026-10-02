"""Uses a disposable native WebSocket fixture to exercise real TLS verification and SSH socket forwarding."""

import datetime
import json
import queue
import socket
import ssl
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from agents.gaiakeep import transport
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from websockets.sync.server import serve


@pytest.fixture
def tls_fixture(tmp_path, monkeypatch):
    key = ec.generate_private_key(ec.SECP384R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'gateway.test')])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('gateway.test')]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA384()))
    pem, private = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(pem, private)
    captured = []
    frames = queue.Queue()
    def handler(ws):
        try:
            captured.append((ws.request.path, ws.request.headers['cresco_service_key']))
            first = json.loads(ws.recv(timeout=5))
            if ws.request.path == '/api/apisocket':
                ws.send(json.dumps({'status': '10', 'client_rpc_id': first['message_payload']['client_rpc_id'],
                                    'native': 'reply'}))
            else:
                assert first['ident_key'] == 'stream_name'
                ws.send(json.dumps({'status_code': '10', 'status_desc': 'Listener Active'}))
                ws.send(b'GKverified-fixture')
                frames.put(ws.recv(timeout=5))
        finally:
            ws.close()
    listener = serve(handler, '127.0.0.1', 0, ssl=server_context, compression=None)
    worker = threading.Thread(target=listener.serve_forever, daemon=True)
    worker.start()
    port = listener.socket.getsockname()[1]
    @contextmanager
    def tunnel(self, target, gateway_port, timeout):
        assert gateway_port == 8282
        with socket.create_connection(('127.0.0.1', port), timeout=timeout) as channel:
            yield channel
    monkeypatch.setattr(transport.ParamikoTransport, 'open_tunnel', tunnel)
    monkeypatch.setattr(transport, 'validate_egress_url', lambda url: None)
    config = transport.GatewayConfig('r:a:p', frozenset({'r:a:p'}), 'gateway.test', str(pem), 'pin', 'key')
    native = transport.NativeTransport(SimpleNamespace(address='registered.machine'), config)
    try:
        yield native, captured, frames
    finally:
        native.close()
        listener.shutdown()
        worker.join(timeout=5)


def test_verified_rpc_and_dataplane(tls_fixture):
    native, captured, sent = tls_fixture
    assert native.call('r:a:p', 'core.status', {}, 20)['native'] == 'reply'
    received = queue.Queue()
    stream = native.open_stream('generated-123', received.put)
    assert received.get(timeout=5) == b'GKverified-fixture'
    stream.send(b'GKbounded-request')
    assert sent.get(timeout=5) == b'GKbounded-request'
    stream.close()
    assert captured == [('/api/apisocket', 'key'), ('/api/dataplane', 'key')]
    assert native.context.verify_mode == ssl.CERT_REQUIRED
    assert native.context.check_hostname


def test_untrusted_certificate_is_refused(tls_fixture):
    native, captured, _ = tls_fixture
    native.context = ssl.create_default_context()
    with pytest.raises(ssl.SSLCertVerificationError):
        native.call('r:a:p', 'core.status', {}, 20)
    assert not captured


def test_wrong_hostname_is_refused(tls_fixture):
    native, captured, _ = tls_fixture
    native.config = transport.GatewayConfig('r:a:p', frozenset({'r:a:p'}), 'wrong.test',
                                            native.config.ca_file, 'pin', 'key')
    with pytest.raises(ssl.SSLCertVerificationError):
        native.call('r:a:p', 'core.status', {}, 20)
    assert not captured
