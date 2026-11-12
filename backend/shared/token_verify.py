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


def check_idp_audience(claims: Dict[str, Any], *, extra: Iterable[str] = ()) -> None:
    audience = claims.get("aud")
    values = [audience] if type(audience) is str else audience
    if (type(values) is not list or not values
            or any(type(value) is not str or not value for value in values)):
        raise TokenRefused("audience_malformed")
    accepted = {"account"} | allowed_azps() | {value for value in extra if type(value) is str and value}
    if not accepted.intersection(values):
        raise TokenRefused("audience_mismatch")


def jwks_url_for(issuer: str) -> str:
    return f"{issuer}/protocol/openid-connect/certs"


def with_owner_identity(claims: Dict[str, Any], issuer: str) -> Dict[str, Any]:
    owner = node_identity.canonical_owner_id(issuer, claims.get("sub"))
    stamped = dict(claims)
    stamped[IDP_SUBJECT_CLAIM] = claims["sub"]
    stamped[IDP_ISSUER_CLAIM] = node_identity.normalize_issuer(issuer)
    stamped["sub"] = owner
    return stamped


async def verify_idp_token(
    token: str,
    *,
    extra_azps: Iterable[str] = (),
    extra_issuers: Iterable[str] = (),
    extra_audiences: Iterable[str] = (),
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
    url = jwks_url_for(claimed)
    # Decode errors propagate unwrapped because callers tell expiry from forgery by exception type
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
    payload = dict(payload)
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
    check_idp_audience(payload, extra=extra_audiences)
    try:
        stamped = with_owner_identity(payload, claimed)
    except node_identity.NodeIdentityError:
        raise TokenRefused("subject_unusable") from None
    try:
        node_identity.enforce_owner(stamped["sub"])
    except node_identity.OwnerMismatch:
        raise TokenRefused("owner_mismatch") from None
    except node_identity.NodeIdentityError:
        raise TokenRefused("owner_binding_unavailable") from None
    return stamped
