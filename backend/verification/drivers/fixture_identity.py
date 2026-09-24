"""Signed fixture identities for the in-process harness (backend/shared/jwks_cache.py):
supplies only local JWKS retrieval while the product's real JWT signature, issuer,
and role checks still run; never real Keycloak.
"""

from __future__ import annotations

import os
import time
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt


class FixtureIdentity:
    _active = None

    def __init__(self, run_id: str) -> None:
        from shared import jwks_cache

        if os.environ.get("ASTRAL_ENV") != "development":
            raise ValueError("synthetic verification IAM requires explicit development mode")
        if not run_id.startswith("__verif__"):
            raise ValueError("synthetic verification IAM requires a namespaced run")
        if type(self)._active is not None:
            raise RuntimeError("synthetic verification IAM already has an active owner")
        self.run_id = run_id
        self.authority = f"https://verification.invalid/realms/{uuid.uuid4().hex}"
        self.url = f"{self.authority}/protocol/openid-connect/certs"
        self.kid = f"fixture-{uuid.uuid4().hex}"
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private_key = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        public = key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.jwks = {"keys": [jwk.construct(public, algorithm="RS256").to_dict() | {
            "kid": self.kid, "use": "sig",
        }]}
        self.values = {
            "KEYCLOAK_AUTHORITY": self.authority,
            "KEYCLOAK_CLIENT_ID": "astral-frontend",
            "KEYCLOAK_ALLOWED_AZP": "astral-frontend",
            "USE_MOCK_AUTH": "false",
        }
        self.previous = {key: os.environ.get(key) for key in self.values}
        os.environ.update(self.values)
        self.cache = jwks_cache._cache
        self.cache[self.url] = {"jwks": self.jwks, "fetched_at": time.time()}
        self.closed = False
        type(self)._active = self

    def claims_and_token(self, claims: dict) -> tuple[dict, str]:
        if self.closed or not claims.get("sub", "").startswith(self.run_id + "_"):
            raise ValueError("fixture principal is outside the active run")
        issued = dict(claims) | {
            "iss": self.authority, "azp": "astral-frontend", "aud": "account",
            "iat": int(time.time()), "exp": int(time.time()) + 300,
        }
        token = jwt.encode(issued, self.private_key, algorithm="RS256", headers={"kid": self.kid})
        self.cache[self.url] = {"jwks": self.jwks, "fetched_at": time.time()}
        return issued, token

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        type(self)._active = None
        self.cache.pop(self.url, None)
        self.private_key = b""
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
