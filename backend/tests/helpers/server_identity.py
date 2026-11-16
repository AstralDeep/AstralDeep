"""Server-profile test identity: signs real RS256 tokens with a fake issuer whose keys are served through shared.jwks_cache, so tests authenticate through the production verifier. Used by the server_identity fixture in tests/conftest.py and imported directly by module suites."""

from __future__ import annotations

from shared import jwks_cache
from shared import node_identity as ni
from shared.token_verify import jwks_url_for
from tests.helpers.idp_tokens import AUTHORITY, CLIENT_ID, FakeIssuer

_UNSET_ENV = (
    "USE_MOCK_AUTH",
    "KEYCLOAK_ALLOWED_AZP",
    "AGENT_SERVICE_CLIENT_ID",
    "JWKS_STALE_GRACE_SECONDS",
    ni.ISSUERS_ENV,
    ni.PROFILE_ENV,
    ni.OWNER_ENV,
)


class ServerIdentity:
    def __init__(self, issuer: FakeIssuer):
        self.issuer = issuer
        self.jwks_url = jwks_url_for(issuer.authority)
        self._online = True

    @classmethod
    def install(cls, monkeypatch, *, data_dir=None) -> "ServerIdentity":
        for name in _UNSET_ENV:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("KEYCLOAK_AUTHORITY", AUTHORITY)
        monkeypatch.setenv("KEYCLOAK_CLIENT_ID", CLIENT_ID)
        if data_dir is not None:
            monkeypatch.setenv(ni.DATA_DIR_ENV, str(data_dir))
        ni.reset_cache()
        identity = cls(FakeIssuer())
        monkeypatch.setattr(jwks_cache, "_cache", {})
        monkeypatch.setattr(jwks_cache, "_fetch", identity._fetch)
        return identity

    async def _fetch(self, url: str) -> dict:
        if not self._online or url != self.jwks_url:
            raise OSError("issuer unreachable")
        jwks_cache._cache[url] = {"jwks": self.issuer.jwks, "fetched_at": jwks_cache.time.time()}
        return self.issuer.jwks

    def take_issuer_offline(self) -> None:
        self._online = False

    def owner(self, subject: str) -> str:
        return ni.canonical_owner_id(self.issuer.authority, subject)

    def token(self, subject: str, roles=("user",), **claims) -> str:
        role_list = list(roles)
        defaults = {
            "preferred_username": subject,
            "realm_access": {"roles": role_list},
            "resource_access": {CLIENT_ID: {"roles": role_list}},
        }
        defaults.update(claims)
        return self.issuer.token(subject, **defaults)

    def headers(self, subject: str, roles=("user",), **claims) -> dict:
        return {"Authorization": f"Bearer {self.token(subject, roles=roles, **claims)}"}
