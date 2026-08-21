"""Feature 028 — durable server-side web-session store (research D3/D5).

Backs ``web_auth``'s signed-cookie sessions with the ``web_session`` Postgres
table so sessions survive backend restarts and multi-instance deploys
(FR-008), honoring the feature-016 365-day hard cap anchored to the last
*interactive* login. Access/refresh tokens are Fernet-encrypted at rest under
``WEB_SESSION_ENC_KEY`` (falling back to ``OFFLINE_GRANT_ENC_KEY``, the
feature-025 convention). In production mode (``ASTRAL_ENV`` != development)
the absence of an encryption key is fail-closed: sessions cannot be persisted
and login is refused rather than storing tokens in the clear.

Also owns ``auth_revocation_queue`` — refresh tokens awaiting best-effort
revocation at Keycloak after an offline sign-out (FR-013; the server-side
analog of 016's client revocation queue).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, Optional

from astralplane.repositories.history import SessionRecord
from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)

logger = logging.getLogger("orchestrator.session_store")

_DEV_VALUES = ("development", "dev")


def is_dev_mode() -> bool:
    """True only when the operator explicitly declared development mode.

    Unset/unknown ``ASTRAL_ENV`` means production: every 028 posture check
    fails closed by default (FR-015/FR-016).
    """
    return os.getenv("ASTRAL_ENV", "").strip().lower() in _DEV_VALUES


# Shipped placeholder values that must never reach production.
_DEV_PLACEHOLDER_SECRETS = (
    "dev-audit-hmac-secret-change-me-in-prod",
    "change-me",
)


def assert_production_posture() -> None:
    """Fail-closed boot gate (028 FR-015, production hardening): refuse to
    serve a production-mode process with a configuration that would silently
    run open or unprotected. Collects EVERY problem before exiting so the
    operator gets one actionable checklist. Raises ``SystemExit(78)``
    (EX_CONFIG) — called from ``Orchestrator.start``.

    Development mode (``ASTRAL_ENV=development``) skips everything except the
    advisory warnings — local dev stays friction-free (spec A13)."""
    mock_on = os.getenv("USE_MOCK_AUTH", "").strip().lower() in ("1", "true", "yes")
    if is_dev_mode():
        return
    problems = []
    if mock_on:
        problems.append(
            "USE_MOCK_AUTH is enabled. Mock authentication accepts any token as "
            "an admin user. Set USE_MOCK_AUTH=false and configure KEYCLOAK_*, "
            "or set ASTRAL_ENV=development (local dev only)."
        )
    if not _enc_key():
        problems.append(
            "WEB_SESSION_ENC_KEY (or OFFLINE_GRANT_ENC_KEY) is unset — durable "
            "web sessions cannot be encrypted at rest. Generate one: python -c "
            "\"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    if not os.getenv("CREDENTIAL_ENCRYPTION_KEY", "").strip():
        problems.append(
            "CREDENTIAL_ENCRYPTION_KEY is unset — OAuth/Fernet credentials would be "
            "encrypted under an auto-generated key that is lost on an ephemeral "
            "volume (silent fail-open). Generate one: python -c \"from "
            "cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    audit_secret = os.getenv("AUDIT_HMAC_SECRET", "").strip()
    if not audit_secret or audit_secret in _DEV_PLACEHOLDER_SECRETS:
        problems.append(
            "AUDIT_HMAC_SECRET is unset or still the shipped dev placeholder — "
            "the audit hash chain would be forgeable. Set a high-entropy value."
        )
    if not mock_on:
        for var, aliases in (
            ("KEYCLOAK_AUTHORITY", ("KEYCLOAK_AUTHORITY",)),
            ("KEYCLOAK_CLIENT_ID", ("KEYCLOAK_CLIENT_ID",)),
            ("KEYCLOAK_CLIENT_SECRET", ()),
        ):
            if not any(os.getenv(name, "").strip() for name in (var, *aliases)):
                problems.append(f"{var} is unset — the OIDC flow cannot operate.")
    agent_key = os.getenv("AGENT_API_KEY", "").strip()
    if agent_key and (agent_key in _DEV_PLACEHOLDER_SECRETS or len(agent_key) < 16):
        problems.append(
            "AGENT_API_KEY is a shipped placeholder or too short (<16 chars) — "
            "set a high-entropy value so agent registrations cannot be forged."
        )
    if problems:
        logger.critical(
            "REFUSING TO START (production posture, ASTRAL_ENV != development) — "
            "fix the following before deploying:\n%s",
            "\n".join(f"  [{i + 1}] {p}" for i, p in enumerate(problems)),
        )
        raise SystemExit(78)  # EX_CONFIG
    if not os.getenv("AGENT_API_KEY", "").strip():
        # Not fatal: agent registrations are refused (fail closed) — but the
        # operator should know no specialist agents will come up.
        logger.warning(
            "AGENT_API_KEY is unset in production mode: ALL agent registrations "
            "will be refused (fail closed, 028 FR-016). Configure it if this "
            "deployment is meant to run specialist agents."
        )


def _enc_key() -> Optional[bytes]:
    raw = os.getenv("WEB_SESSION_ENC_KEY") or os.getenv("OFFLINE_GRANT_ENC_KEY")
    return raw.encode() if raw else None


class SessionStoreError(Exception):
    """Raised when the store cannot operate safely (e.g. no key in prod)."""


class WebSessionStore:
    """Postgres-backed session CRUD with an in-process read-through cache."""

    def __init__(
        self,
        db=None,
        *,
        plane_runtime=None,
        plane_repositories=None,
        session_context: PlaneRepositoryContext | None = None,
        revocation_context: PlaneRepositoryContext | None = None,
    ):
        """Bind to the application's initialized Plane runtime.

        ``db`` remains accepted only as a transition-time dependency carrier:
        it must expose the already-created ``plane_runtime`` and repository
        catalog.  It is never used for statements or connection ownership.
        Tests may instead inject the two narrow repository contexts directly.
        """
        if session_context is None:
            history, runtime = repository_from(
                "history",
                plane_runtime=plane_runtime,
                repositories=plane_repositories,
                legacy_database=db,
            )
            if runtime is None:
                raise ValueError("an initialized Plane runtime is required")
            session_context = PlaneRepositoryContext(
                repository=history.sessions,
                plane_runtime=runtime,
            )
        if revocation_context is None:
            revocations, runtime = repository_from(
                "revocations",
                plane_runtime=plane_runtime,
                repositories=plane_repositories,
                legacy_database=db,
            )
            if runtime is None:
                raise ValueError("an initialized Plane runtime is required")
            revocation_context = PlaneRepositoryContext(
                repository=revocations,
                plane_runtime=runtime,
            )
        self._sessions = session_context
        self._revocations = revocation_context
        self._cache: Dict[str, Dict[str, Any]] = {}
        # Resolution and attempt mutations are owner-scoped in Plane.  The
        # trusted drainer first obtains these fences from pending_revocations;
        # keeping them detached here prevents queue ids becoming authority.
        self._revocation_fences: Dict[int, tuple[str, int]] = {}
        # sid -> why get() last returned None for it ('hard_cap'), so the
        # /auth/session contract can report reason:'hard_cap' (auth-session.md).
        self._death_reasons: Dict[str, str] = {}
        self._fernet = None
        key = _enc_key()
        if key:
            try:
                from cryptography.fernet import Fernet
                self._fernet = Fernet(key)
            except Exception:
                logger.exception("session_store: invalid WEB_SESSION_ENC_KEY (must be urlsafe-base64 Fernet)")
                self._fernet = None
        if self._fernet is None and not is_dev_mode():
            # Fail closed: production sessions must never hit disk unencrypted.
            raise SessionStoreError(
                "WEB_SESSION_ENC_KEY (or OFFLINE_GRANT_ENC_KEY) is required outside "
                "development mode — refusing to run with unencrypted session storage."
            )
        if self._fernet is None:
            logger.warning("session_store: DEV MODE — sessions stored without encryption at rest")

    # ── crypto ───────────────────────────────────────────────────────────
    def _enc(self, value: str) -> str:
        if self._fernet is None:
            return value or ""
        return self._fernet.encrypt((value or "").encode()).decode()

    def _dec(self, value: str) -> str:
        if self._fernet is None:
            return value or ""
        try:
            return self._fernet.decrypt((value or "").encode()).decode()
        except Exception:
            logger.warning("session_store: token decrypt failed (key rotated?) — treating session as dead")
            return ""

    def _from_record(self, record: SessionRecord) -> Dict[str, Any]:
        return {
            "sid": record.session_id,
            "user_id": record.owner_id,
            "access_token": self._dec(record.access_token_ciphertext),
            "refresh_token": self._dec(record.refresh_token_ciphertext),
            "interactive_anchor": record.interactive_anchor,
            "hard_expires_at": record.hard_expires_at,
            "last_refresh_at": record.last_refresh_at,
            "resumed": record.resumed,
            "created_at": record.created_at,
        }

    # ── session CRUD ─────────────────────────────────────────────────────
    def create(self, sid: str, *, user_id: str, access_token: str,
               refresh_token: str, hard_max_seconds: int,
               resumed: bool = False) -> Dict[str, Any]:
        """Persist a new interactive session. Only this call sets the anchor."""
        now = int(time.time())
        row = {
            "sid": sid,
            "user_id": user_id,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "interactive_anchor": now,
            "hard_expires_at": now + int(hard_max_seconds),
            "last_refresh_at": now,
            "resumed": bool(resumed),
            "created_at": now,
        }
        record = SessionRecord(
            session_id=sid,
            owner_id=user_id,
            access_token_ciphertext=self._enc(access_token),
            refresh_token_ciphertext=self._enc(refresh_token),
            interactive_anchor=row["interactive_anchor"],
            hard_expires_at=row["hard_expires_at"],
            last_refresh_at=row["last_refresh_at"],
            resumed=row["resumed"],
            created_at=row["created_at"],
        )
        self._sessions.call(self._sessions.repository.put, record=record)
        self._cache[sid] = row
        return row

    def get(self, sid: str) -> Optional[Dict[str, Any]]:
        """Return the live session (cap-checked); expired sessions are deleted."""
        row = self._cache.get(sid)
        if row is None:
            with self._sessions.transaction() as transaction:
                record = self._sessions.repository.get_by_session_id_for_administration(
                    transaction,
                    session_id=sid,
                )
                if record is None:
                    return None
                row = self._from_record(record)
                if not row["access_token"] and not row["refresh_token"]:
                    # Undecryptable (key rotation) — dead session.
                    self._sessions.repository.delete(
                        transaction,
                        owner_id=record.owner_id,
                        session_id=record.session_id,
                    )
                    return None
            if row is None:  # pragma: no cover - narrows Optional for type checkers
                return None
            self._cache[sid] = row
        if int(time.time()) >= row["hard_expires_at"]:
            # 016 hard cap: only interactive login can start a new session.
            logger.info("session_store: session %s hit the 365-day cap — cleared", sid[:8])
            self.delete(sid)
            self._record_death(sid, "hard_cap")
            return None
        return row

    def latest_refresh_token_for(self, user_id: str) -> Optional[str]:
        """The live refresh token of the user's newest interactive session.

        056 (D8/FR-011): the explicit consent-capture step needs the user's
        ``offline_access`` refresh token to create a durable offline grant, and
        the encrypted web session is where it already lives — so consent
        capture reads it from here instead of the product ever holding a second
        copy. Returns ``None`` when the user has no live session (capture then
        fails closed and nothing durable is created). Token bytes never leave
        this class except through this deliberate, consent-gated read.
        """
        with self._sessions.transaction() as transaction:
            record = self._sessions.repository.get_latest_live_for_owner(
                transaction,
                owner_id=user_id,
                observed_at=int(time.time()),
            )
            if record is None:
                return None
            row = self._from_record(record)
            if not row["access_token"] and not row["refresh_token"]:
                self._sessions.repository.delete(
                    transaction,
                    owner_id=record.owner_id,
                    session_id=record.session_id,
                )
                self._cache.pop(record.session_id, None)
                return None
        self._cache[record.session_id] = row
        return row["refresh_token"] or None

    def _record_death(self, sid: str, reason: str) -> None:
        if len(self._death_reasons) > 256:
            self._death_reasons.clear()
        self._death_reasons[sid] = reason

    def pop_death_reason(self, sid: str) -> Optional[str]:
        """Why get() last refused this sid ('hard_cap'), consumed on read."""
        return self._death_reasons.pop(sid, None)

    def update_tokens(self, sid: str, *, access_token: str, refresh_token: str) -> None:
        """Rotate tokens after a silent refresh. NEVER moves the anchor (016 FR-001)."""
        with self._sessions.transaction() as transaction:
            current = self._sessions.repository.get_by_session_id_for_administration(
                transaction,
                session_id=sid,
            )
            if current is None:
                self._cache.pop(sid, None)
                return
            refreshed_at = max(int(time.time()), current.last_refresh_at + 1)
            refreshed = SessionRecord(
                session_id=current.session_id,
                owner_id=current.owner_id,
                access_token_ciphertext=self._enc(access_token),
                refresh_token_ciphertext=self._enc(refresh_token),
                interactive_anchor=current.interactive_anchor,
                hard_expires_at=current.hard_expires_at,
                last_refresh_at=refreshed_at,
                resumed=current.resumed,
                created_at=current.created_at,
            )
            stored = self._sessions.repository.compare_and_set_refresh(
                transaction,
                refreshed,
                expected_last_refresh_at=current.last_refresh_at,
            )
        self._cache[sid] = self._from_record(stored)

    def mark_resumed(self, sid: str, resumed: bool = True) -> None:
        target = bool(resumed)
        with self._sessions.transaction() as transaction:
            current = self._sessions.repository.get_by_session_id_for_administration(
                transaction,
                session_id=sid,
            )
            if current is None:
                self._cache.pop(sid, None)
                return
            stored = (
                current
                if current.resumed == target
                else self._sessions.repository.mark_resumed(
                    transaction,
                    owner_id=current.owner_id,
                    session_id=current.session_id,
                    expected_resumed=current.resumed,
                    resumed=target,
                )
            )
        self._cache[sid] = self._from_record(stored)

    def delete(self, sid: str) -> Optional[Dict[str, Any]]:
        """Delete a session; returns the cached row (for revocation) if known."""
        row = self._cache.pop(sid, None)
        with self._sessions.transaction() as transaction:
            record = self._sessions.repository.get_by_session_id_for_administration(
                transaction,
                session_id=sid,
            )
            if record is None:
                return row
            if row is None:
                row = self._from_record(record)
            self._sessions.repository.delete(
                transaction,
                owner_id=record.owner_id,
                session_id=record.session_id,
            )
        return row

    def delete_for_user(self, user_id: str) -> int:
        """Delete every session of a user (user-switch revocation, 016 FR-008)."""
        for sid in [s for s, r in self._cache.items() if r.get("user_id") == user_id]:
            self._cache.pop(sid, None)
        return self._sessions.call(
            self._sessions.repository.delete_owner,
            owner_id=user_id,
        )

    def purge_expired(self) -> int:
        """Opportunistic cleanup of hard-cap-expired rows."""
        now = int(time.time())
        for sid in [s for s, r in self._cache.items() if now >= r.get("hard_expires_at", 0)]:
            self._cache.pop(sid, None)
        return self._sessions.call(
            self._sessions.repository.delete_expired_for_administration,
            observed_at=now,
        )

    # ── revocation queue (FR-013; client_id added by feature 044) ────────
    def enqueue_revocation(self, user_id: str, refresh_token: str,
                           client_id: str | None = None) -> None:
        if not refresh_token:
            return
        self._revocations.call(
            self._revocations.repository.enqueue,
            owner_id=user_id,
            refresh_token_ciphertext=self._enc(refresh_token),
            enqueued_at=int(time.time()),
            client_id=(client_id or "").strip() or None,
        )

    def pending_revocations(self, limit: int = 20) -> list:
        records = self._revocations.call(
            self._revocations.repository.pending_for_administration,
            limit=limit,
        )
        out = []
        fences: Dict[int, tuple[str, int]] = {}
        for record in records:
            fences[record.queue_id] = (record.owner_id, record.attempts)
            out.append({
                "id": record.queue_id,
                "user_id": record.owner_id,
                "refresh_token": self._dec(record.refresh_token_ciphertext),
                "attempts": record.attempts,
                "enqueued_at": record.enqueued_at,
                # NULL for pre-044 rows → retrier falls back to the web client id.
                "client_id": record.client_id,
            })
        self._revocation_fences = fences
        return out

    def resolve_revocation(self, queue_id: int) -> None:
        try:
            owner_id, _attempts = self._revocation_fences.pop(queue_id)
        except KeyError:
            raise SessionStoreError(
                "revocation mutation requires a pending owner fence"
            ) from None
        self._revocations.call(
            self._revocations.repository.resolve,
            owner_id=owner_id,
            queue_id=queue_id,
        )

    def bump_revocation_attempt(self, queue_id: int) -> None:
        try:
            owner_id, attempts = self._revocation_fences[queue_id]
        except KeyError:
            raise SessionStoreError(
                "revocation mutation requires a pending owner fence"
            ) from None
        updated = self._revocations.call(
            self._revocations.repository.bump_attempt,
            owner_id=owner_id,
            queue_id=queue_id,
            expected_attempts=attempts,
        )
        self._revocation_fences[queue_id] = (owner_id, updated.attempts)

    # ── async facade (event-loop-safe twins of the sync methods above) ────
    async def acreate(self, sid: str, *, user_id: str, access_token: str,
                      refresh_token: str, hard_max_seconds: int,
                      resumed: bool = False) -> Dict[str, Any]:
        """Async twin of :meth:`create`, run off the event loop."""
        return await asyncio.to_thread(
            self.create, sid, user_id=user_id, access_token=access_token,
            refresh_token=refresh_token, hard_max_seconds=hard_max_seconds,
            resumed=resumed,
        )

    async def aget(self, sid: str) -> Optional[Dict[str, Any]]:
        """Async twin of :meth:`get`, run off the event loop."""
        return await asyncio.to_thread(self.get, sid)

    async def aupdate_tokens(self, sid: str, *, access_token: str, refresh_token: str) -> None:
        """Async twin of :meth:`update_tokens`, run off the event loop."""
        return await asyncio.to_thread(
            self.update_tokens, sid, access_token=access_token, refresh_token=refresh_token
        )

    async def amark_resumed(self, sid: str, resumed: bool = True) -> None:
        """Async twin of :meth:`mark_resumed`, run off the event loop."""
        return await asyncio.to_thread(self.mark_resumed, sid, resumed)

    async def adelete(self, sid: str) -> Optional[Dict[str, Any]]:
        """Async twin of :meth:`delete`, run off the event loop."""
        return await asyncio.to_thread(self.delete, sid)

    async def adelete_for_user(self, user_id: str) -> int:
        """Async twin of :meth:`delete_for_user`, run off the event loop."""
        return await asyncio.to_thread(self.delete_for_user, user_id)

    async def apurge_expired(self) -> int:
        """Async twin of :meth:`purge_expired`, run off the event loop."""
        return await asyncio.to_thread(self.purge_expired)

    async def aenqueue_revocation(self, user_id: str, refresh_token: str,
                                  client_id: str | None = None) -> None:
        """Async twin of :meth:`enqueue_revocation`, run off the event loop."""
        return await asyncio.to_thread(self.enqueue_revocation, user_id, refresh_token, client_id)

    async def apending_revocations(self, limit: int = 20) -> list:
        """Async twin of :meth:`pending_revocations`, run off the event loop."""
        return await asyncio.to_thread(self.pending_revocations, limit)

    async def aresolve_revocation(self, queue_id: int) -> None:
        """Async twin of :meth:`resolve_revocation`, run off the event loop."""
        return await asyncio.to_thread(self.resolve_revocation, queue_id)

    async def abump_revocation_attempt(self, queue_id: int) -> None:
        """Async twin of :meth:`bump_revocation_attempt`, run off the event loop."""
        return await asyncio.to_thread(self.bump_revocation_attempt, queue_id)
