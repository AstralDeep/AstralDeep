"""Verifies signed client composition, honest outcomes, credential containment and bounded file conveniences."""

import base64
import gzip
import hashlib
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from agents.gaiakeep import client
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec


class Core:
    def __init__(self):
        self.calls = []
        self.closed = False

    def call(self, action, params, timeout=None):
        self.calls.append((action, params))
        return {'status': '10', 'job_id': 'native-job'}

    def ingest(self, files, **kwargs):
        self.path = next(iter(files.values()))
        assert Path(self.path).read_bytes() == b'hello'
        return SimpleNamespace(vid='native-version', state='published')

    def read(self, vid, path, max_bytes=None):
        assert max_bytes == client.MAX_FILE
        return b'hello'

    def close(self):
        self.closed = True


def test_actions_preserve_job():
    core = Core()
    assert client.execute(core, 'core.repair', {})['job_id'] == 'native-job'
    assert core.calls[0][0] == 'core.repair'


def test_invalid_native_json_is_not_an_argument_error():
    core = Core()
    core.call = lambda *a, **kw: {'profiles': '{invalid'}
    with pytest.raises(client.ProtocolError) as error:
        client.execute(core, 'core.profile', {})
    assert client.failure(error.value, True)[0] == 'unconfirmed'


def test_legalorder_shortening_refused():
    core = Core()
    with pytest.raises(client.AgentError) as error:
        client.execute(core, 'core.legalorder', {'action': 'shorten'})
    assert error.value.verdict == 'unsupported'
    assert not core.calls


def test_upload_cleanup():
    core = Core()
    out = client.upload(core, 'c', 'file.txt', base64.b64encode(b'hello').decode(), 'main')
    assert out['vid'] == 'native-version'
    assert not Path(core.path).exists()


def test_read_bytes():
    out = client.read(Core(), 'v', 'file.txt')
    assert base64.b64decode(out['data_base64']) == b'hello'
    assert out['byte_count'] == 5
    assert len(out['sha256']) == 64


@pytest.mark.parametrize('data', ['!', base64.b64encode(b'x' * (client.MAX_FILE + 1)).decode()], ids=['invalid', 'oversize'])
def test_upload_bounds(data):
    with pytest.raises(client.AgentError):
        client.upload(Core(), 'c', 'file.txt', data, 'main')


def test_recursive_secrets():
    out = client.clean_result({'sig': 'signature', 'nested': {'key': 'credential'},
                               'text': 'echo credential', 'value': 4}, ['credential'])
    assert 'credential' not in str(out)
    assert 'signature' not in str(out)
    assert out['value'] == 4


def credentials(curve=None):
    key = ec.generate_private_key(curve or ec.SECP384R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return {'GAIAKEEP_PRINCIPAL': 'principal', 'GAIAKEEP_PRIVATE_KEY': pem}, key


def test_real_identity():
    creds, key = credentials()
    principal, parsed = client.credential_identity(creds)
    assert principal == 'principal'
    assert parsed.private_numbers() == key.private_numbers()


def test_single_line_key_for_standard_credentials_surface():
    creds, key = credentials()
    creds['GAIAKEEP_PRIVATE_KEY'] = base64.b64encode(creds['GAIAKEEP_PRIVATE_KEY'].encode()).decode()
    principal, parsed = client.credential_identity(creds)
    assert principal == 'principal'
    assert parsed.private_numbers() == key.private_numbers()


def test_invalid_encoded_key():
    creds = {'GAIAKEEP_PRINCIPAL': 'principal', 'GAIAKEEP_PRIVATE_KEY': base64.b64encode(b'invalid').decode()}
    with pytest.raises(client.AgentError) as error:
        client.credential_identity(creds)
    assert error.value.verdict == 'auth_failed'


@pytest.mark.parametrize('creds', [{}, {'GAIAKEEP_PRINCIPAL': 1},
                                    {'GAIAKEEP_PRINCIPAL': 'p'}, {'GAIAKEEP_PRINCIPAL': 'p', 'GAIAKEEP_PRIVATE_KEY': 'invalid'}])
def test_invalid_identity(creds):
    with pytest.raises(client.AgentError):
        client.credential_identity(creds)


def test_wrong_curve():
    with pytest.raises(client.AgentError):
        client.credential_identity(credentials(ec.SECP256R1())[0])


def test_sdk_provenance_refused(monkeypatch):
    monkeypatch.setattr(client.importlib.metadata, 'distribution',
                        lambda name: SimpleNamespace(read_text=lambda field: '{"vcs_info":{"commit_id":"wrong"}}'))
    with pytest.raises(client.AgentError) as error:
        client.load_sdk()
    assert error.value.verdict == 'not_configured'


def test_missing_config_and_cleanup_on_constructor_failure(monkeypatch):
    def constructor(*args, **kwargs):
        raise ValueError('invalid-core-pin')
    monkeypatch.setattr(client, 'load_sdk', lambda: (constructor, lambda **kwargs: kwargs, None, None))
    creds, _ = credentials()
    monkeypatch.setattr(client.GatewayConfig, 'from_environment', lambda: (_ for _ in ()).throw(ValueError()))
    with pytest.raises(client.AgentError), client.open_client(object(), creds):
        pass
    closed = []
    monkeypatch.setattr(client, 'NativeTransport', lambda *a: SimpleNamespace(close=lambda: closed.append(True)))
    with pytest.raises(ValueError), client.open_client(object(), creds, SimpleNamespace(core_address='r:a:p', core_public_key='bad')):
        pass
    assert closed == [True]


def test_legacy_opt_in_and_peer_audience_refused():
    core = SimpleNamespace(signer=SimpleNamespace(principal='p'),
                           transport=SimpleNamespace(call=lambda *a: {'status': '10'}))
    with pytest.raises(client.AgentError):
        client.execute(core, 'listnodes', {})
    config = SimpleNamespace(allow_legacy=True, core_address='r:a:p')
    with pytest.raises(client.AgentError):
        client.execute(core, 'reconstructauth', {'key_id': 'k'}, config)


def test_legacy_restore_confinement_and_incomplete_fetch_refusal(monkeypatch):
    monkeypatch.setattr(client, 'load_sdk', lambda: (None, None, None, lambda action, reply: reply))
    captured = []
    def call(*args):
        captured.append(args)
        return {'status': '10', 'job_id': 'accepted-job'}
    core = SimpleNamespace(signer=SimpleNamespace(principal='p'), transport=SimpleNamespace(call=call))
    config = SimpleNamespace(allow_legacy=True, core_address='r:a:p', restore_root='/srv/gaia-restore')
    assert client.execute(core, 'restore', {'object_id': 'o', 'dest_path': 'relative'}, config,
                          {'GAIAKEEP_RECONSTRUCTION_TOKEN': 'credential-token'})['job_id'] == 'accepted-job'
    assert captured[-1][2]['dest_path'] == '/srv/gaia-restore/relative'
    assert captured[-1][2]['user'] == 'p'
    assert captured[-1][2]['reconstruct_token'] == 'credential-token'
    with pytest.raises(ValueError):
        client.execute(core, 'restore', {'object_id': 'o', 'dest_path': '../escape'}, config)
    config.restore_root = '/'
    with pytest.raises(client.AgentError):
        client.execute(core, 'restore', {'object_id': 'o', 'dest_path': 'relative'}, config)
    with pytest.raises(client.AgentError):
        client.execute(core, 'fetch', {}, config)
    before = len(captured)
    with pytest.raises(client.AgentError) as error:
        client.execute(core, 'fetch', {}, config, {'GAIAKEEP_FETCH_GRANT': 'encrypted-grant'})
    assert error.value.verdict == 'unsupported'
    assert len(captured) == before


def test_idempotency_and_events_owned():
    core = Core()
    client.execute(core, 'core.commit', {'upload_id': 'u', 'request_id': 'existing-request-id', 'events': 'bad-stream'})
    assert core.calls[-1][1]['request_id'] == 'existing-request-id'
    assert 'events' not in core.calls[-1][1]
    client.execute(core, 'core.repair', {})
    assert len(core.calls[-1][1]['request_id']) == 32


def test_dedup_upload_and_failure_cleanup():
    core = Core()
    def have(files, **kwargs):
        core.path = next(iter(files.values()))
        assert Path(core.path).read_bytes() == b'hello'
        assert kwargs['expected_head'] == 'current-version'
        return {'vid': 'v'}, {'reused': 1}
    core.have_ingest = have
    assert client.upload(core, 'c', 'p', base64.b64encode(b'hello').decode(), 'main', 'have',
                         expected_head='current-version')['publication']['vid'] == 'v'
    assert not Path(core.path).exists()
    def fail(files, **kwargs):
        core.path = next(iter(files.values()))
        raise OSError('interrupted')
    core.ingest = fail
    with pytest.raises(OSError):
        client.upload(core, 'c', 'p', base64.b64encode(b'hello').decode(), 'main')
    assert not Path(core.path).exists()


def test_result_and_read_bounds():
    core = Core()
    core.read = lambda *a, **k: b'x' * (client.MAX_FILE + 1)
    with pytest.raises(client.AgentError):
        client.read(core, 'v', 'p')
    assert client.clean_result({'credential-value': 'value'}, ['credential-value']) == {'[redacted]': 'value'}
    assert client.clean_result('[1,true,null]') == [1, True, None]
    for value in [object(), {'many': [0] * 10001}, 'x' * (client.MAX_EXPANDED + 1)]:
        with pytest.raises(client.AgentError):
            client.clean_result(value)


@pytest.mark.parametrize('kind,mutation,expected', [
    ('Unauthenticated', True, 'auth_failed'), ('IntegrityError', False, 'integrity_error'),
    ('Forbidden', True, 'upstream_denied'), ('RpcTimeout', True, 'unconfirmed'),
    ('TransportError', False, 'unavailable')])
def test_truthful_failure_labels(kind, mutation, expected):
    exc = type(kind, (Exception,), {})('private-exception-payload')
    verdict, message = client.failure(exc, mutation)
    assert verdict == expected
    assert 'private-exception-payload' not in message


def test_bounded_result_failure_cases():
    for value in [float('nan'), {str(i): i for i in range(10001)},
                  [[[[[[[[[[[[[[[[[[1]]]]]]]]]]]]]]]]]], '{broken']:
        if value == '{broken':
            assert client.clean_result(value) == value
        else:
            with pytest.raises(client.AgentError):
                client.clean_result(value)
    encoded = base64.b64encode(gzip.compress(b'{"sig":"auth","value":1}')).decode()
    assert client.clean_result(encoded) == {'value': 1}
    assert client.failure(client.ProtocolError(), False)[0] == 'protocol_error'


def test_control_cannot_open_dangling_file_sessions():
    with pytest.raises(client.AgentError):
        client.execute(Core(), 'core.put', {})


def test_native_reply_validation_and_legalorder_erase():
    core = Core()
    client.execute(core, 'core.legalorder', {'action': 'erase'})
    assert 'action' not in core.calls[-1][1]
    core.call = lambda *a, **k: []
    with pytest.raises(client.ProtocolError):
        client.execute(core, 'core.status', {})
    core.call = lambda *a, **k: {'status': '10', 'files': base64.b64encode(gzip.compress(b'{"p":{}}')).decode()}
    assert client.execute(core, 'core.list', {'vid': 'v'})['files'] == {'p': {}}
    core.call = lambda *a, **k: {'status': '10', 'files': '{"p":{}}'}
    assert client.execute(core, 'core.list', {'vid': 'v'})['files'] == {'p': {}}


def test_pinned_extraction_certificate():
    key = ec.generate_private_key(ec.SECP384R1())
    cert = '{"leaves":1,"root":"commitment","creator":"principal"}'
    def framed(prefix):
        a, b = prefix.encode(), cert.encode()
        return struct.pack('>I', len(a)) + a + struct.pack('>I', len(b)) + b
    signature = key.sign(framed('GFS-XCERT-1'), ec.ECDSA(hashes.SHA384()))
    public = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    reply = {'status': '10', 'cert': cert, 'sig': base64.urlsafe_b64encode(signature).decode().rstrip('='),
             'signer_pub': base64.urlsafe_b64encode(public).decode().rstrip('='), 'core_public_key': 'untrusted-echo'}
    extract_id = hashlib.sha384(framed('gfs/extract/v2')).hexdigest()[:48]
    core = SimpleNamespace(core_key=key.public_key(), call=lambda *a, **k: dict(reply))
    result = client.clean_result(client.execute(core, 'core.extractproof', {'extract_id': extract_id, 'leaf': '0'}))
    proof = result['verified_certificate']
    assert proof['certificate_verified'] is True
    assert proof['leaf_inclusion_verified'] is False
    assert base64.b64decode(proof['certificate_utf8_base64']).decode() == cert
    assert proof['certificate_signature'] == reply['sig']
    assert 'sig' not in result
    for change in [{'cert': cert + ' '}, {'sig': 'bad'}, {'signer_pub': 'wrong'}, {'cert': 'x' * 65537}]:
        core.call = lambda *a, _change=change, **k: dict(reply, **_change)
        with pytest.raises(client.AgentError) as error:
            client.execute(core, 'core.extractproof', {'extract_id': extract_id, 'leaf': '0'})
        assert error.value.verdict == 'integrity_error'


def test_redacted_certificate_never_retains_verified_claim():
    proof = {'certificate_verified': True, 'leaf_inclusion_verified': False,
             'certificate_utf8_base64': 'Y2VydA==', 'certificate_signature': 'signed-artifact',
             'certificate_public_key': 'public-key'}
    assert client.clean_result(proof, ['unrelated-password']) == proof
    assert client.clean_result(proof, ['Y']) == {
        'certificate_verified': False, 'leaf_inclusion_verified': False, 'artifact_omitted': True}
