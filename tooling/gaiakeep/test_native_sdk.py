"""Qualifies the separately installed private SDK against the agent's signing and cleanup boundaries."""

import base64
from types import SimpleNamespace

from agents.gaiakeep import client
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def credentials():
    key = ec.generate_private_key(ec.SECP384R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return {'GAIAKEEP_PRINCIPAL': 'principal', 'GAIAKEEP_PRIVATE_KEY': pem}, key


def test_exact_dependency_is_installed():
    assert client.load_sdk()[0].__name__ == 'CoreClient'


def test_real_signed_core_composition_and_cleanup(monkeypatch):
    from gaiakeep.signing import verify
    creds, key = credentials()
    core_key = ec.generate_private_key(ec.SECP384R1()).public_key()
    public = base64.urlsafe_b64encode(core_key.public_bytes(serialization.Encoding.DER,
                                                           serialization.PublicFormat.SubjectPublicKeyInfo)).decode().rstrip('=')
    calls = []
    class Fabric:
        def __init__(self, target, config):
            self.closed = False
            calls.append(self)
        def call(self, addr, action, params, timeout):
            if action == 'core.status':
                return {'status': '10', 'audience': 'verified-audience'}
            signed = verify(key.public_key(), action, params)
            assert signed['audience'] == 'verified-audience'
            assert params['principal'] == 'principal'
            return {'status': '10', 'owner': 'principal'}
        def close(self):
            self.closed = True
    monkeypatch.setattr(client, 'NativeTransport', Fabric)
    config = SimpleNamespace(core_address='r:a:p', core_public_key=public)
    with client.open_client(object(), creds, config) as (core, actual):
        assert actual is config
        assert core.call('core.whoami')['owner'] == 'principal'
    assert calls[0].closed


def test_peer_bound_prototype_signing(monkeypatch):
    from gaiakeep.signing import Signer, verify
    _, key = credentials()
    signer = Signer('principal', key, audience='global-audience')
    calls = []
    def call(addr, action, params, timeout):
        calls.append((action, params))
        if action == 'core.status':
            return {'status': '10', 'peer_audience': 'peer-only'}
        signed = verify(key.public_key(), action, params)
        assert signed['audience'] == 'peer-only'
        assert signed['user'] == 'principal'
        return {'status': '10', 'native_job_id': 'job'}
    core = SimpleNamespace(signer=signer, transport=SimpleNamespace(call=call))
    config = SimpleNamespace(core_address='r:a:p', allow_legacy=True)
    assert client.execute(core, 'reconstructauth', {'key_id': 'k'}, config)['native_job_id'] == 'job'
    assert calls[-1][0] == 'reconstructauth'
