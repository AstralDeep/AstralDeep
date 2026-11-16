"""Exercises publisher registration through normal RSA/JWKS user authentication without dependency overrides.
Verified issuer/subject stamps must select exactly one canonical registry/audit owner; nodes wake with capability alone."""

from __future__ import annotations

import base64
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

from orchestrator.mesh_push_registry import MeshPushError
from shared.node_identity import canonical_owner_id
from tests.test_mesh_push_api import DEVICE, ISSUER, MESH, host  # noqa: F401
from tests.test_mesh_push_registry import plane  # noqa: F401


@pytest.fixture
def real_auth(host, monkeypatch):  # noqa: F811
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                               serialization.NoEncryption())
    numbers = private.public_key().public_numbers()

    def encoded(value):
        return base64.urlsafe_b64encode(value.to_bytes((value.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()

    keys = {"keys": [{"kty": "RSA", "kid": "test", "alg": "RS256", "n": encoded(numbers.n), "e": encoded(numbers.e)}]}
    fetched = []

    async def jwks(url, **kwargs):
        fetched.append(url)
        return keys

    monkeypatch.delenv("ASTRAL_NODE_PROFILE", raising=False)
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", ISSUER)
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", "astral-mobile")
    monkeypatch.setenv("ASTRAL_ACCEPTED_ISSUERS", ISSUER + ",https://other.example/realms/owners")
    monkeypatch.setattr("shared.jwks_cache.get_jwks", jwks)
    host.app.dependency_overrides.clear()

    def credential(**changes):
        claims = {"iss": ISSUER, "sub": host.principal["astral_idp_sub"], "aud": "account", "azp": "astral-mobile",
                  "exp": int(time.time()) + 300, "realm_access": {"roles": ["user"]}}
        claims.update(changes)
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})

    return credential, fetched


def registration(service, token):
    return service.client.post("/api/push/mesh/destinations", headers={"Authorization": "Bearer " + token},
                            json={"mesh_id": MESH, "device_id": DEVICE, "token": "private-phone-token", "expected_revision": 0})


def test_real_verified_principal_registers_under_exact_canonical_owner_and_wakes_without_iam(host, real_auth):  # noqa: F811
    token, fetched = real_auth
    response = registration(host, token(astral_idp_sub="forged-original-subject"))
    assert response.status_code == 200
    owner = canonical_owner_id(ISSUER, host.principal["astral_idp_sub"])
    assert host.store.status(owner, mesh_id=MESH, device_id=DEVICE).state == "active"
    with pytest.raises(MeshPushError, match="registration_missing"):
        host.store.status(canonical_owner_id(ISSUER, owner), mesh_id=MESH, device_id=DEVICE)
    count = len(fetched)
    wake = host.client.post("/api/push/mesh/wake", headers={"Authorization": "Bearer " + response.json()["route"]["capability"]},
                            json={"mesh_id": MESH, "device_id": DEVICE})
    assert wake.status_code == 204 and host.adapter.tokens == ["private-phone-token"]
    assert len(fetched) == count
    events, _ = host.store.audit_repository.list_for_user(owner)
    assert {event.action_type for event in events} == {"push.mesh.register", "push.mesh.wake.authorized"}
    other_events, _ = host.store.audit_repository.list_for_user(canonical_owner_id(ISSUER, "unrelated-subject"))
    assert other_events == []
    assert all("private-phone-token" not in event.model_dump_json() and response.json()["route"]["capability"] not in event.model_dump_json()
               for event in events)


def test_verified_same_subject_on_two_issuers_cannot_rotate_or_observe_another_owner(host, real_auth):  # noqa: F811
    token, _ = real_auth
    assert registration(host, token()).status_code == 200
    other = "https://other.example/realms/owners"
    response = registration(host, token(iss=other))
    assert response.status_code == 200
    assert host.store.status(canonical_owner_id(ISSUER, host.principal["astral_idp_sub"]), mesh_id=MESH, device_id=DEVICE).revision == 1
    assert host.store.status(canonical_owner_id(other, host.principal["astral_idp_sub"]), mesh_id=MESH, device_id=DEVICE).revision == 1


@pytest.mark.parametrize("claims", [{"aud": "unrelated-api"}, {"azp": "untrusted-client"},
                                     {"realm_access": {"roles": []}}, {"iss": "https://unaccepted.example"},
                                     {"act": {"sub": "agent"}}, {"exp": 1}])
def test_normal_user_auth_denials_never_register_a_destination(host, real_auth, claims):  # noqa: F811
    token, _ = real_auth
    response = registration(host, token(**claims))
    assert response.status_code in {401, 403} and host.adapter.tokens == []


@pytest.mark.parametrize("field", ["iss", "sub"])
def test_inconsistent_verified_principal_stamp_is_refused(host, field):  # noqa: F811
    corrupted = dict(host.principal)
    corrupted[field] = "different-owner-or-issuer"
    from orchestrator.auth import verify_user

    host.app.dependency_overrides[verify_user] = lambda: corrupted
    assert registration(host, "ignored-by-explicit-fixture").status_code == 401
    assert host.adapter.tokens == []
