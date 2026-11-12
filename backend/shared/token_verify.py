"""The one verification function for Keycloak-issued access tokens: accepted issuer, required azp, canonical (issuer, subject) owner id and personal-node owner binding.
orchestrator/auth.py, orchestrator.validate_token and shared/a2a_security.py all call it so the three paths cannot drift.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Iterable, Optional

from jose import jwt as jose_jwt

from shared import node_identity
from shared.auth_clients import allowed_azps
from shared import jwks_cache

IDP_SUBJECT_CLAIM = "astral_idp_sub"
IDP_ISSUER_CLAIM = "astral_idp_iss"


class TokenRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def jwks_url_for(issuer: str) -> str:
    return f"{issuer}/protocol/openid-connect/certs"


async def verify_idp_token(
    token: str,
    *,
    extra_azps: Iterable[str] = (),
    extra_issuers: Iterable[str] = (),
    jwks_getter: Optional[Callable[[str, str], Awaitable[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    if not isinstance(token, str) or not token:
        raise TokenRefused("missing_token")
    issuers = node_identity.accepted_issuers(extra_issuers)
    if not issuers:
        raise TokenRefused("no_accepted_issuer")
    claimed = issuers[0]
    if len(issuers) > 1:
        try:
            peeked = node_identity.normalize_issuer(jose_jwt.get_unverified_claims(token).get("iss"))
        except Exception:
            peeked = ""
        if peeked in issuers:
            claimed = peeked
    try:
        url = jwks_url_for(claimed)
        if jwks_getter is not None:
            jwks = await jwks_getter(url, token)
        else:
            jwks = await jwks_cache.get_jwks(url, token=token)
        payload = jose_jwt.decode(
            token,
            jwks,
            algorithms=["RS256"],
            options={"verify_aud": False, "verify_at_hash": False, "require_exp": True},
        )
    except Exception:
        raise TokenRefused("signature_or_expiry") from None
    token_issuer = node_identity.normalize_issuer(payload.get("iss"))
    if not token_issuer:
        raise TokenRefused("missing_issuer")
    if token_issuer != claimed:
        raise TokenRefused("issuer_not_accepted")
    azp = payload.get("azp")
    if not isinstance(azp, str) or not azp:
        raise TokenRefused("missing_azp")
    if azp not in allowed_azps() | {a for a in extra_azps if a}:
        raise TokenRefused("azp_not_accepted")
    try:
        owner = node_identity.canonical_owner_id(claimed, payload.get("sub"))
    except node_identity.NodeIdentityError:
        raise TokenRefused("subject_unusable") from None
    try:
        node_identity.enforce_owner(owner)
    except node_identity.OwnerMismatch:
        raise TokenRefused("owner_mismatch") from None
    except node_identity.NodeIdentityError:
        raise TokenRefused("owner_binding_unavailable") from None
    payload[IDP_SUBJECT_CLAIM] = payload["sub"]
    payload[IDP_ISSUER_CLAIM] = claimed
    payload["sub"] = owner
    return payload
