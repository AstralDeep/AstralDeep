"""Encrypted offline-grant store for unattended job authorization (feature 025).

⚠️ SECURITY-CRITICAL — gated by task T057 (lead-dev security review) before merge.

At consent time (user present, live session) we store an encrypted reference to
the user's canonical Keycloak session credential and a hard 365-day grant cap.
Browser and background refreshes share one durable claim/rotation sequence.
Per run, the scheduler:
  1. loads the grant; refuses if revoked / expired (FR-024),
  2. exchanges the refresh token at Keycloak for a fresh short-lived access token,
  3. (caller then) intersects the job's consented scopes with the user's CURRENT
     scopes and performs the existing RFC 8693 delegated exchange.

The grant reference uses ``OFFLINE_GRANT_ENC_KEY``; the canonical credential
remains encrypted under the existing web-session key. Tokens are never returned
by an API or logged. Legacy copied grants convert only on an exact live-session
match. Unknown refresh outcomes require fresh sign-in and renewed consent;
potentially consumed tokens are never retried.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Optional

import aiohttp

from agentic_settings import OFFLINE_GRANT_ENC_KEY, OFFLINE_GRANT_MAX_DAYS
from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)

logger = logging.getLogger("orchestrator.offline_grant")

_DAY_MS = 86_400_000
_SESSION_REFERENCE_PREFIX = "\x00astral-offline-session/v1\x00"
_REFRESH_HTTP_SECONDS = 10
_REFRESH_BODY_BYTES = 65536
_GRANT_MINT_SECONDS = 20


class OfflineGrantError(RuntimeError):
    """Raised when a grant cannot be captured, is unavailable, or is expired/revoked."""


class TokenEndpointUnconfigured(OfflineGrantError):
    """The IdP token endpoint cannot be derived from the deployment's config.

    An operator problem, not a consent problem: no refresh exchange is
    attempted and the machine turn must fail closed with a reason that names
    the missing setting instead of blaming the user's consent.
    """


def resolve_token_endpoint() -> str:
    """Return the Keycloak token endpoint for refresh exchanges.

    Precedence:
      1. ``KEYCLOAK_TOKEN_URL`` — explicit full-URL override.
      2. ``KEYCLOAK_AUTHORITY`` (the realm URL every other Keycloak caller
         uses, e.g. ``https://idp.example/realms/Astral``) +
         ``/protocol/openid-connect/token``.
      3. Legacy ``KEYCLOAK_URL`` + ``/realms/<KEYCLOAK_REALM>`` — only when
         ``KEYCLOAK_URL`` is explicitly set (no silent ``'astral'`` realm
         guess against an empty host, which used to yield
         ``/realms/astral/...`` — a relative, unusable URL).

    Raises ``TokenEndpointUnconfigured`` when nothing usable is configured.
    """

    def _http_url(value: str) -> bool:
        return value.startswith("https://") or value.startswith("http://")

    explicit = (os.getenv("KEYCLOAK_TOKEN_URL") or "").strip()
    if explicit:
        if not _http_url(explicit):
            raise TokenEndpointUnconfigured(
                "KEYCLOAK_TOKEN_URL must be an absolute http(s) URL"
            )
        return explicit

    authority = (os.getenv("KEYCLOAK_AUTHORITY") or "").strip().rstrip("/")
    if authority:
        if not _http_url(authority):
            raise TokenEndpointUnconfigured(
                "KEYCLOAK_AUTHORITY must be an absolute http(s) realm URL"
            )
        return f"{authority}/protocol/openid-connect/token"

    legacy_base = (os.getenv("KEYCLOAK_URL") or "").strip().rstrip("/")
    if legacy_base and _http_url(legacy_base):
        realm = (os.getenv("KEYCLOAK_REALM") or "").strip()
        if realm:
            return f"{legacy_base}/realms/{realm}/protocol/openid-connect/token"
        raise TokenEndpointUnconfigured(
            "KEYCLOAK_URL is set but KEYCLOAK_REALM is empty; set "
            "KEYCLOAK_AUTHORITY (preferred) or KEYCLOAK_REALM"
        )

    raise TokenEndpointUnconfigured(
        "no IdP token endpoint configured: set KEYCLOAK_AUTHORITY "
        "(realm URL) or KEYCLOAK_TOKEN_URL"
    )


_APPLICATION_STORE = None


def bind_offline_grant_store(store) -> None:
    """Publish the application-scoped store for request modules without an owner object."""

    global _APPLICATION_STORE
    if store is None:
        raise ValueError("offline grant store binding is required")
    _APPLICATION_STORE = store


def unbind_offline_grant_store(store) -> None:
    """Release only the exact application-scoped offline-grant binding."""

    global _APPLICATION_STORE
    if _APPLICATION_STORE is None:
        return
    if _APPLICATION_STORE is not store:
        raise RuntimeError("offline grant store unbind does not own the binding")
    _APPLICATION_STORE = None


def get_offline_grant_store():
    if _APPLICATION_STORE is None:
        raise RuntimeError("offline grant persistence has not been bound to AstralPlane")
    return _APPLICATION_STORE


def _fernet():
    """Build a Fernet from the configured key, or raise (fail closed)."""
    if not OFFLINE_GRANT_ENC_KEY:
        raise OfflineGrantError(
            "OFFLINE_GRANT_ENC_KEY is not configured; refusing to store offline grants."
        )
    from cryptography.fernet import Fernet  # already present via python-jose[cryptography] chain
    return Fernet(OFFLINE_GRANT_ENC_KEY.encode() if isinstance(OFFLINE_GRANT_ENC_KEY, str) else OFFLINE_GRANT_ENC_KEY)


def _now_ms() -> int:
    return int(time.time() * 1000)


class OfflineGrantStore:
    """Persistence + crypto for offline grants. Token bytes never leave this class."""

    def __init__(
        self,
        db=None,
        *,
        plane_runtime=None,
        plane_repositories=None,
        plane_repository=None,
    ) -> None:
        if db is None and plane_runtime is None:
            raise ValueError("OfflineGrantStore requires the application Plane runtime")
        repository, runtime = repository_from(
            "offline_grants",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=db,
        )
        self._grants = PlaneRepositoryContext(
            repository=plane_repository or repository,
            plane_runtime=runtime,
            legacy_database=db,
        )

    def capture(self, user_id: str, refresh_token: str, agent_id: Optional[str] = None) -> str:
        """Encrypt a reference to the consented live session credential family.

        Returns the new grant id. Raises OfflineGrantError if encryption is not
        configured (fail closed — never store plaintext).
        """
        if not refresh_token:
            raise OfflineGrantError("no refresh token available in the session (offline_access not granted)")
        cipher = _fernet()
        reference = self._session_reference(user_id, refresh_token)
        token_enc = cipher.encrypt((_SESSION_REFERENCE_PREFIX + json.dumps(
            reference, sort_keys=True, separators=(",", ":"))).encode())
        grant_id = str(uuid.uuid4())
        now = _now_ms()
        expires = now + OFFLINE_GRANT_MAX_DAYS * _DAY_MS
        self._grants.call(
            self._grants.repository.create_grant,
            grant_id=grant_id,
            owner_id=user_id,
            agent_id=agent_id,
            encrypted_refresh_token=token_enc,
            issued_at=now,
            expires_at=expires,
        )
        return grant_id

    def _sessions(self):
        from orchestrator.session_store import WebSessionStore
        return WebSessionStore(plane_runtime=self._grants.plane_runtime)

    def _session_reference(self, user_id, refresh_token):
        from orchestrator.session_store import SessionStoreError
        try:
            return self._sessions().session_reference(user_id, refresh_token)
        except SessionStoreError:
            raise OfflineGrantError("live session credential required; re-consent required") from None

    def _resolve_reference(self, grant):
        try:
            plaintext = _fernet().decrypt(grant.encrypted_refresh_token).decode()
        except Exception:
            raise OfflineGrantError("offline grant credential cannot be decrypted") from None
        if not plaintext.startswith(_SESSION_REFERENCE_PREFIX):
            # A legacy copied token is never sent to the IdP. Convert only an
            # exact current-session match, otherwise request fresh consent.
            reference = self._session_reference(grant.owner_id, plaintext)
            ciphertext = _fernet().encrypt((_SESSION_REFERENCE_PREFIX + json.dumps(
                reference, sort_keys=True, separators=(",", ":"))).encode())
            updated = self._grants.call(
                self._grants.repository.replace_refresh_token_if_current,
                owner_id=grant.owner_id, grant_id=grant.grant_id,
                expected_encrypted_refresh_token=grant.encrypted_refresh_token,
                encrypted_refresh_token=ciphertext, as_of=_now_ms())
            if updated is None:
                raise OfflineGrantError("offline grant changed; retry with current authority")
            return reference
        try:
            reference = json.loads(plaintext[len(_SESSION_REFERENCE_PREFIX):])
            if (not isinstance(reference, dict)
                    or set(reference) != {"session_id", "created_at", "interactive_anchor"}
                    or not isinstance(reference["session_id"], str)
                    or not 1 <= len(reference["session_id"]) <= 1024
                    or any(type(reference[k]) is not int or reference[k] < 0
                           for k in ("created_at", "interactive_anchor"))):
                raise ValueError
            return reference
        except (ValueError, TypeError):
            raise OfflineGrantError("offline grant session reference is malformed") from None

    def _grant(self, user_id: str, grant_id: str):
        return self._grants.call(
            self._grants.repository.get_grant,
            owner_id=user_id,
            grant_id=grant_id,
        )

    def revoke_for_user(self, user_id: str) -> int:
        """Revoke all of a user's grants (e.g. on logout / sign-out-everywhere)."""
        return self._grants.call(
            self._grants.repository.revoke_owner,
            owner_id=user_id,
            revoked_at=_now_ms(),
        )

    def is_valid(self, grant_id: str, *, user_id: str) -> bool:
        """Check a grant only within the authenticated owner's namespace."""

        grant = self._grant(user_id, grant_id)
        if grant is None:
            return False
        if not grant.active:
            return False
        if grant.expires_at <= _now_ms():
            return False
        return True

    def latest_valid_for(self, user_id: str, agent_id: Optional[str] = None) -> Optional[str]:
        """Most recent unrevoked, unexpired grant id for the user.

        Prefers a grant captured for ``agent_id`` when one exists, else falls
        back to the user's newest valid grant of any agent. Returns only the
        id — token bytes never leave this class. Used by 056's
        ``MachineTurnAuthority`` so machine-turn classes without an explicit
        job-linked grant (parser replay, draft self-tests) can still derive
        authority from the user's standing consent, and skip fail-closed when
        none exists.
        """
        reference = self._grants.call(
            self._grants.repository.find_latest_valid,
            owner_id=user_id,
            agent_id=agent_id,
            as_of=_now_ms(),
        )
        return None if reference is None else reference.grant_id

    async def mint_access_token(self, grant_id: str, *, user_id: str) -> str:
        """Bound the entire fresh-authority path, including durable acquisition."""
        try:
            async with asyncio.timeout(_GRANT_MINT_SECONDS):
                return await self._mint_access_token(grant_id, user_id=user_id)
        except TimeoutError:
            raise OfflineGrantError("offline grant mint time limit exceeded") from None

    async def _mint_access_token(self, grant_id: str, *, user_id: str) -> str:
        """Exchange the stored refresh token for a fresh access token at Keycloak.

        Raises OfflineGrantError on revoked/expired grants or refresh failure
        (e.g. Keycloak-side revocation) — the caller fails the run safe.
        """
        grant = await asyncio.to_thread(self._grant, user_id, grant_id)
        if grant is None:
            raise OfflineGrantError("offline grant not found")
        if not grant.active:
            raise OfflineGrantError("offline grant revoked")
        if grant.expires_at <= _now_ms():
            raise OfflineGrantError("offline grant expired (365-day cap reached); re-consent required")

        # Resolve the endpoint BEFORE touching the refresh token: a
        # misconfigured deployment must fail closed without decrypting
        # anything (and without a doomed HTTP call).
        token_url = resolve_token_endpoint()

        reference = await asyncio.to_thread(self._resolve_reference, grant)
        from orchestrator.session_store import SessionStoreError
        from orchestrator.web_auth import _session_client_id

        async def exchange(refresh_token, prior_access):
            current = await asyncio.to_thread(self._grant, user_id, grant_id)
            if current is None or not current.active or current.expires_at <= _now_ms():
                raise OfflineGrantError("offline grant revoked or expired")
            web_client = os.getenv("KEYCLOAK_CLIENT_ID") or "astral-frontend"
            client_id = _session_client_id({"access_token": prior_access}) or web_client
            data = {"grant_type": "refresh_token", "client_id": client_id,
                    "refresh_token": refresh_token}
            client_secret = os.getenv("KEYCLOAK_CLIENT_SECRET")
            if client_secret and client_id == web_client:
                data["client_secret"] = client_secret
            timeout = aiohttp.ClientTimeout(total=_REFRESH_HTTP_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(token_url, data=data, allow_redirects=False) as resp:
                    if resp.status != 200:
                        logger.warning("offline_grant.refresh_failed", extra={"status": resp.status})
                        raise OfflineGrantError(f"refresh exchange failed ({resp.status})")
                    try:
                        body = await resp.content.readexactly(_REFRESH_BODY_BYTES + 1)
                    except asyncio.IncompleteReadError as ended:
                        body = ended.partial
                    if len(body) > _REFRESH_BODY_BYTES:
                        raise OfflineGrantError("refresh exchange response exceeds limit")
                    try:
                        return json.loads(body)
                    except (ValueError, UnicodeError):
                        raise OfflineGrantError("refresh exchange response is malformed") from None

        try:
            row = await self._sessions().refresh_credential(
                reference["session_id"], owner_id=user_id, exchange=exchange,
                reference=reference)
        except (SessionStoreError, aiohttp.ClientError, TimeoutError):
            raise OfflineGrantError("session refresh unavailable; fresh sign-in may be required") from None
        current = await asyncio.to_thread(self._grant, user_id, grant_id)
        if current is None or not current.active or current.expires_at <= _now_ms():
            raise OfflineGrantError("offline grant revoked or expired during refresh")
        access_token = row["access_token"]
        # 030 FR-017: structured observability for grant mints (success path).
        logger.info("offline_grant.minted",
                    extra={"grant_id": grant_id, "user_id": grant.owner_id})
        return access_token
