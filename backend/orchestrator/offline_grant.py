"""Encrypted offline-grant store for unattended job authorization: encrypts, at consent
time, a reference to the user's Keycloak session under a 365-day cap, for the
scheduler to exchange later for a fresh access token. Used by scheduler/store.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from agentic_settings import OFFLINE_GRANT_ENC_KEY, OFFLINE_GRANT_MAX_DAYS
from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)
from orchestrator.session_consent import ConsentSession

logger = logging.getLogger("orchestrator.offline_grant")

_DAY_MS = 86_400_000
_SESSION_REFERENCE_PREFIX = "\x00astral-offline-session/v2\x00"
_REFRESH_HTTP_SECONDS = 10
_REFRESH_BODY_BYTES = 65536
_GRANT_MINT_SECONDS = 20


class OfflineGrantError(RuntimeError):
    pass


class TokenEndpointUnconfigured(OfflineGrantError):
    pass


@dataclass(frozen=True)
class PreparedConsentGrant:
    owner_id: str
    selected_session: ConsentSession = field(repr=False)
    grant_id: str
    encrypted_reference: bytes = field(repr=False)
    agent_id: Optional[str]
    plane_runtime: object = field(repr=False)


def resolve_token_endpoint() -> str:
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
    global _APPLICATION_STORE
    if store is None:
        raise ValueError("offline grant store binding is required")
    _APPLICATION_STORE = store


def unbind_offline_grant_store(store) -> None:
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
    if not OFFLINE_GRANT_ENC_KEY:
        raise OfflineGrantError(
            "OFFLINE_GRANT_ENC_KEY is not configured; refusing to store offline grants."
        )
    from cryptography.fernet import Fernet
    return Fernet(OFFLINE_GRANT_ENC_KEY.encode() if isinstance(OFFLINE_GRANT_ENC_KEY, str) else OFFLINE_GRANT_ENC_KEY)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _reference_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate reference field")
        result[key] = value
    return result


class OfflineGrantStore:
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

    def capture(self, user_id: str, selected_session: ConsentSession,
                agent_id: Optional[str] = None) -> str:
        prepared = self.prepare_capture(user_id, selected_session, agent_id)
        try:
            with self._grants.transaction() as transaction:
                self.capture_in_transaction(transaction, prepared,
                    plane_runtime=self._grants.plane_runtime)
                self.assert_current_capture(transaction, prepared,
                    plane_runtime=self._grants.plane_runtime)
        except Exception:
            raise OfflineGrantError("live consenting session required; re-consent required") from None
        return prepared.grant_id

    def prepare_capture(self, user_id: str, selected_session: ConsentSession,
                        agent_id: Optional[str] = None) -> PreparedConsentGrant:
        if not isinstance(selected_session, ConsentSession):
            raise OfflineGrantError("live consenting session required; re-consent required")
        cipher = _fernet()
        reference = self._session_reference(user_id, selected_session)
        encrypted = cipher.encrypt(self._reference_bytes(reference))
        return PreparedConsentGrant(user_id, selected_session, str(uuid.uuid4()),
                                    encrypted, agent_id, self._grants.plane_runtime)

    @staticmethod
    def _reference_bytes(reference):
        return (_SESSION_REFERENCE_PREFIX + json.dumps(
            reference, sort_keys=True, separators=(",", ":"))).encode()

    def assert_current_capture(self, transaction, prepared, *, plane_runtime):
        try:
            if (not isinstance(prepared, PreparedConsentGrant)
                    or prepared.plane_runtime is not self._grants.plane_runtime
                    or plane_runtime is not prepared.plane_runtime):
                raise ValueError
            reference = prepared.selected_session.reference(prepared.owner_id)
            identity = uuid.UUID(prepared.grant_id)
            if (identity.version != 4 or str(identity) != prepared.grant_id
                    or _fernet().decrypt(prepared.encrypted_reference) != self._reference_bytes(reference)):
                raise ValueError
            sessions = plane_runtime.repositories.history.sessions
            sessions.bound_request_execution_waits(transaction)
            return sessions.assert_current_consent(
                transaction, observation=prepared.selected_session.observation)
        except Exception:
            raise OfflineGrantError("live consenting session required; re-consent required") from None

    def capture_in_transaction(self, transaction, prepared, *, plane_runtime) -> str:
        current = self.assert_current_capture(transaction, prepared, plane_runtime=plane_runtime)
        now = int(current.observed_at.timestamp() * 1000)
        self._grants.repository.create_grant(
            transaction, grant_id=prepared.grant_id, owner_id=prepared.owner_id,
            agent_id=prepared.agent_id, encrypted_refresh_token=prepared.encrypted_reference,
            issued_at=now, expires_at=now + OFFLINE_GRANT_MAX_DAYS * _DAY_MS)
        return prepared.grant_id

    def _sessions(self):
        from orchestrator.session_store import WebSessionStore
        return WebSessionStore(plane_runtime=self._grants.plane_runtime)

    def _session_reference(self, user_id, selected_session):
        from orchestrator.session_store import SessionStoreError
        try:
            expected = selected_session.reference(user_id)
            reference = self._sessions().session_reference(
                user_id, session_id=expected["session_id"],
                incarnation_id=expected["incarnation_id"])
            if reference != expected:
                raise ValueError("session reference changed")
            selected_session.reference(user_id)
            return reference
        except (SessionStoreError, ValueError):
            raise OfflineGrantError("live session credential required; re-consent required") from None

    def _resolve_reference(self, grant):
        try:
            plaintext = _fernet().decrypt(grant.encrypted_refresh_token).decode()
        except Exception:
            raise OfflineGrantError("offline grant credential cannot be decrypted") from None
        if not plaintext.startswith(_SESSION_REFERENCE_PREFIX):
            raise OfflineGrantError("legacy offline grant requires re-consent")
        try:
            reference = json.loads(plaintext[len(_SESSION_REFERENCE_PREFIX):], object_pairs_hook=_reference_object)
            if (not isinstance(reference, dict)
                    or set(reference) != {"session_id", "incarnation_id", "created_at", "interactive_anchor"}
                    or not isinstance(reference["session_id"], str)
                    or not 1 <= len(reference["session_id"]) <= 1024
                    or not isinstance(reference["incarnation_id"], str)
                    or uuid.UUID(reference["incarnation_id"]).version != 4
                    or str(uuid.UUID(reference["incarnation_id"])) != reference["incarnation_id"]
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
        return self._grants.call(
            self._grants.repository.revoke_owner,
            owner_id=user_id,
            revoked_at=_now_ms(),
        )

    def is_valid(self, grant_id: str, *, user_id: str) -> bool:
        grant = self._grant(user_id, grant_id)
        if grant is None:
            return False
        if not grant.active:
            return False
        if grant.expires_at <= _now_ms():
            return False
        return True

    def latest_valid_for(self, user_id: str, agent_id: Optional[str] = None) -> Optional[str]:
        reference = self._grants.call(
            self._grants.repository.find_latest_valid,
            owner_id=user_id,
            agent_id=agent_id,
            as_of=_now_ms(),
        )
        return None if reference is None else reference.grant_id

    async def mint_access_token(self, grant_id: str, *, user_id: str) -> str:
        try:
            async with asyncio.timeout(_GRANT_MINT_SECONDS):
                return await self._mint_access_token(grant_id, user_id=user_id)
        except TimeoutError:
            raise OfflineGrantError("offline grant mint time limit exceeded") from None

    async def _mint_access_token(self, grant_id: str, *, user_id: str) -> str:
        grant = await asyncio.to_thread(self._grant, user_id, grant_id)
        if grant is None:
            raise OfflineGrantError("offline grant not found")
        if not grant.active:
            raise OfflineGrantError("offline grant revoked")
        if grant.expires_at <= _now_ms():
            raise OfflineGrantError("offline grant expired (365-day cap reached); re-consent required")

        # Resolve endpoint first: fail closed before decrypting
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

        async def bound_exchange(refresh_token, identity):
            from orchestrator.web_auth import _exchange_bound_session_refresh
            current = await asyncio.to_thread(self._grant, user_id, grant_id)
            if current is None or not current.active or current.expires_at <= _now_ms():
                raise OfflineGrantError("offline grant revoked or expired")
            try:
                payload = await _exchange_bound_session_refresh(refresh_token, identity)
            except Exception:
                raise OfflineGrantError("session refresh unavailable; fresh sign-in may be required") from None
            current = await asyncio.to_thread(self._grant, user_id, grant_id)
            if current is None or not current.active or current.expires_at <= _now_ms():
                raise OfflineGrantError("offline grant revoked or expired during refresh")
            return payload

        try:
            row = await self._sessions().refresh_credential(
                reference["session_id"], owner_id=user_id, exchange=exchange,
                reference=reference, bound_exchange=bound_exchange)
        except (SessionStoreError, aiohttp.ClientError, TimeoutError):
            raise OfflineGrantError("session refresh unavailable; fresh sign-in may be required") from None
        current = await asyncio.to_thread(self._grant, user_id, grant_id)
        if current is None or not current.active or current.expires_at <= _now_ms():
            raise OfflineGrantError("offline grant revoked or expired during refresh")
        access_token = row["access_token"]
        logger.info("offline_grant.minted",
                    extra={"grant_id": grant_id, "user_id": grant.owner_id})
        return access_token
