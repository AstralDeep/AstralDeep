"""Qualifies the separately installed private SDK against the agent's signing, sealing and cleanup boundaries."""

import base64
import json
import time
from types import SimpleNamespace

import pytest
from agents.gaiakeep import client
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec


def credentials():
    key = ec.generate_private_key(ec.SECP384R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return {'GAIAKEEP_PRINCIPAL': 'principal', 'GAIAKEEP_PRIVATE_KEY': pem}, key


def public(key):
    return base64.urlsafe_b64encode(key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode().rstrip('=')


class SealingFabric:
    def __init__(self, client_key, advertise=True):
        from gaiakeep import seal
        self.client_key, self.advertise, self.calls, self.closed = client_key, advertise, [], False
        self.identity, self.seal_key = ec.generate_private_key(ec.SECP384R1()), ec.generate_private_key(ec.SECP384R1())
        pub, expires = public(self.seal_key), int(time.time() * 1000) + 600000
        kid = seal.kid(pub)
        statement = seal.advert_bytes('verified-audience', 'r:a:p', kid, pub, expires, public(self.identity))
        self.advert = {'seal_pub': pub, 'seal_pub_kid': kid, 'seal_expires': str(expires), 'seal_peer': 'r:a:p',
                       'seal_identity': public(self.identity),
                       'seal_sig': seal.b64url(self.identity.sign(statement, ec.ECDSA(hashes.SHA384())))}

    def __call__(self, target, config):
        return self

    def call(self, addr, action, params, timeout):
        from gaiakeep import aead, seal
        from gaiakeep.signing import verify
        self.calls.append((action, dict(params)))
        if action == 'core.status':
            return dict({'status': '10', 'audience': 'verified-audience'}, **(self.advert if self.advertise else {}))
        kid, epk, salt = params['seal_kid'], params['seal_epk'], params['seal_salt']
        shared = self.seal_key.exchange(ec.ECDH(), seal.parse_public(epk))
        inner = json.loads(aead.open_once(seal.key(shared, seal.unb64url(salt), seal.REQ, kid, epk),
                                          seal.aad(seal.REQ, action, kid, epk, salt), seal.unb64url(params['seal_body'])))
        signed = verify(self.client_key.public_key(), action, inner)
        assert signed['audience'] == 'verified-audience' and inner['principal'] == 'principal'
        body = json.dumps({'status': '10', 'owner': inner['principal']}).encode()
        sealed = aead.seal_once(seal.key(shared, seal.unb64url(salt), seal.RSP, kid, epk),
                                seal.aad(seal.RSP, action, kid, epk, salt), body)
        return {'status': '10', 'seal_reply': seal.b64url(sealed)}

    def close(self):
        self.closed = True


def test_exact_dependency_is_installed():
    assert client.load_sdk()[0].__name__ == 'CoreClient'


def test_real_signed_core_composition_is_sealed_and_cleaned_up(monkeypatch):
    creds, key = credentials()
    fabric = SealingFabric(key)
    monkeypatch.setattr(client, 'NativeTransport', fabric)
    config = SimpleNamespace(core_address='r:a:p', core_public_key=public(fabric.identity))
    with client.open_client(object(), creds, config) as (core, actual):
        assert actual is config
        assert core.call('core.whoami')['owner'] == 'principal'
        assert core.seal_mode == 'require' and core.seal_stats['sealed'] == 1 and core.seal_stats['plaintext'] == 0
    whoami = [params for action, params in fabric.calls if action == 'core.whoami']
    assert len(whoami) == 1 and 'principal' not in whoami[0] and 'sig' not in whoami[0]
    assert fabric.closed


def test_real_sdk_sends_nothing_signed_without_a_seal_key(monkeypatch):
    creds, key = credentials()
    fabric = SealingFabric(key, advertise=False)
    monkeypatch.setattr(client, 'NativeTransport', fabric)
    config = SimpleNamespace(core_address='r:a:p', core_public_key=public(fabric.identity))
    with client.open_client(object(), creds, config) as (core, _):
        with pytest.raises(Exception) as raised:
            core.call('core.whoami')
    assert type(raised.value).__name__ == 'SealUnavailable'
    assert client.failure(raised.value, False)[0] == 'unavailable'
    assert {action for action, _ in fabric.calls} == {'core.status'} and fabric.closed
