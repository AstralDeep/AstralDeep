"""Signs real RS256 access tokens for one or more fake issuers and serves their key sets through shared.jwks_cache, so identity tests exercise the real decode path."""

from __future__ import annotations

import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt

AUTHORITY = "https://idp.example/realms/astral"
CLIENT_ID = "astral-frontend"


class FakeIssuer:
    def __init__(self, authority: str = AUTHORITY, kid: str = "k1"):
        self.authority = authority
        self.kid = kid
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._pem = self._key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        public = self._key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        entry = jwk.construct(public.decode(), "RS256").to_dict()
        entry.update({"kid": kid, "use": "sig", "alg": "RS256"})
        self.jwks = {"keys": [entry]}

    def token(self, subject: str = "user-1", **overrides) -> str:
        now = int(time.time())
        claims = {
            "sub": subject,
            "iss": self.authority,
            "azp": CLIENT_ID,
            "aud": "account",
            "iat": now,
            "exp": now + 300,
            "realm_access": {"roles": ["user"]},
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, self._pem, algorithm="RS256", headers={"kid": self.kid})


def serve(monkeypatch, *issuers: FakeIssuer, unreachable: bool = False):
    by_url = {f"{i.authority}/protocol/openid-connect/certs": i.jwks for i in issuers}

    async def get_jwks(url, token=None):
        if unreachable:
            raise OSError("issuer unreachable")
        return by_url[url]

    monkeypatch.setattr("shared.jwks_cache.get_jwks", get_jwks)
    return by_url
