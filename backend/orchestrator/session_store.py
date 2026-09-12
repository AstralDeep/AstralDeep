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
import json
import logging
import os
import secrets
import time
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from uuid import UUID

from astralplane.repositories import RepositoryConflictError, RepositoryNotFoundError
from astralplane.repositories.history import (
    SessionCredentialFence,
    SessionExecutionObservation,
    SessionExecutionState,
    SessionRecord,
    SessionRepository,
)
from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)

logger = logging.getLogger("orchestrator.session_store")

_DEV_VALUES = ("development", "dev")
REFRESH_WAIT_SECONDS = 15.0
_REFRESH_CLAIM_PREFIX = "\x00astral-session-refresh/v1\x00"
_TOKEN_MAX_BYTES = 32768


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
    (EX_CONFIG) — called before command-line runtime construction and again
    from ``Orchestrator.start`` for embedded callers.

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


class SessionRefreshUnavailable(SessionStoreError):
    """Refresh cannot safely continue; existing access may still be usable."""


def _valid_token(value: object) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= _TOKEN_MAX_BYTES
            and value.isascii() and all(32 < ord(ch) < 127 for ch in value))


def _valid_incarnation(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = UUID(value)
        return parsed.version == 4 and str(parsed) == value
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class WebSessionReference:
    """Private request-local observation of one durable issued incarnation."""

    state: SessionExecutionState = field(repr=False)


@dataclass(frozen=True, slots=True)
class RefreshedSessionCredential:
    """Persisted rotation awaiting normal IAM verification; authorizes no work."""

    credential: SessionCredentialFence = field(repr=False)
    started_at: datetime
    access_token: str = field(repr=False)


def _refresh_payload_valid(payload: object) -> bool:
    return (isinstance(payload, dict) and _valid_token(payload.get("access_token"))
            and ("refresh_token" not in payload or _valid_token(payload["refresh_token"])))


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
        self._cache_lock = threading.RLock()
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
            return "" if (value or "").startswith(_REFRESH_CLAIM_PREFIX) else value or ""
        try:
            plaintext = self._fernet.decrypt((value or "").encode()).decode()
            # A claim is authenticated ciphertext, never an OAuth credential.
            return "" if plaintext.startswith(_REFRESH_CLAIM_PREFIX) else plaintext
        except Exception:
            logger.warning("session_store: token decrypt failed (key rotated?) — treating session as dead")
            return ""

    def _from_record(self, record: SessionRecord) -> Dict[str, Any]:
        return {
            "sid": record.session_id,
            "incarnation_id": record.incarnation_id,
            "user_id": record.owner_id,
            "access_token": self._dec(record.access_token_ciphertext),
            "refresh_token": self._dec(record.refresh_token_ciphertext),
            "interactive_anchor": record.interactive_anchor,
            "hard_expires_at": record.hard_expires_at,
            "last_refresh_at": record.last_refresh_at,
            "resumed": record.resumed,
            "created_at": record.created_at,
        }

    def _evict_cached(self, sid: str, *, incarnation_id=None, observed=None) -> None:
        """Retire only the cached observation selected before a database wait."""
        with self._cache_lock:
            current = self._cache.get(sid)
            if current is observed or (
                incarnation_id is not None and current is not None
                and current.get("incarnation_id") == incarnation_id
            ):
                self._cache.pop(sid, None)

    def _remember_row(self, row: dict, observed=None) -> None:
        """Cache a read result only while it cannot replace a newer issuance."""
        with self._cache_lock:
            current = self._cache.get(row["sid"])
            if current is observed or (
                current is not None and current.get("incarnation_id") == row["incarnation_id"]
            ):
                self._cache[row["sid"]] = row

    # ── session CRUD ─────────────────────────────────────────────────────
    def create(self, sid: str, *, user_id: str, access_token: str,
               refresh_token: str, hard_max_seconds: int,
               resumed: bool = False) -> Dict[str, Any]:
        """Persist a new interactive session. Only this call sets the anchor."""
        observed = self._cache.get(sid)
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
        stored = self._sessions.call(self._sessions.repository.put, record=record)
        row = self._from_record(stored)
        self._remember_row(row, observed)
        return row

    def get(self, sid: str) -> Optional[Dict[str, Any]]:
        """Return the live session (cap-checked); expired sessions are deleted."""
        # Other workers rotate this exact credential family. A process cache
        # cannot decide token validity or whether a logout has deleted a row.
        observed = self._cache.get(sid)
        with self._sessions.transaction() as transaction:
            record = self._sessions.repository.get_by_session_id_for_administration(
                transaction, session_id=sid)
            if record is None:
                self._evict_cached(sid, observed=observed)
                return None
            row = self._from_record(record)
            if not row["access_token"] and not row["refresh_token"]:
                self._sessions.repository.delete(
                    transaction, owner_id=record.owner_id, session_id=record.session_id,
                    expected_incarnation_id=record.incarnation_id)
                self._evict_cached(sid, incarnation_id=record.incarnation_id)
                return None
        self._remember_row(row, observed)
        if int(time.time()) >= row["hard_expires_at"]:
            # 016 hard cap: only interactive login can start a new session.
            logger.info("session_store: session %s hit the 365-day cap — cleared", sid[:8])
            self.delete(sid, expected_incarnation_id=record.incarnation_id)
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
                    expected_incarnation_id=record.incarnation_id,
                )
                self._evict_cached(record.session_id, incarnation_id=record.incarnation_id)
                return None
        return row["refresh_token"] or None

    def _record_death(self, sid: str, reason: str) -> None:
        if len(self._death_reasons) > 256:
            self._death_reasons.clear()
        self._death_reasons[sid] = reason

    def session_reference(self, owner_id: str, *, session_id: str, incarnation_id: str) -> dict:
        """Resolve an approving request's exact issued session, never its latest."""
        if not _valid_incarnation(incarnation_id) or self._fernet is None:
            raise SessionRefreshUnavailable("live encrypted session required")
        with self._request_execution_transaction() as transaction:
            record = self._sessions.repository.get_by_incarnation(
                transaction, owner_id=owner_id, incarnation_id=incarnation_id)
        current = self._dec(record.refresh_token_ciphertext) if record is not None else ""
        if (record is None or record.session_id != session_id
                or record.hard_expires_at <= int(time.time()) or not _valid_token(current)):
            raise SessionRefreshUnavailable("selected session is unavailable")
        return {"session_id": record.session_id, "incarnation_id": record.incarnation_id,
                "created_at": record.created_at,
                "interactive_anchor": record.interactive_anchor}

    def is_current_incarnation(self, owner_id: str, *, session_id: str, incarnation_id: str) -> bool:
        """Check offline tolerance against the original issuance, without tokens.

        This unlocked read is not execution authority. Durable mutations still
        require their independently validated and transaction-fenced observation.
        """
        if not _valid_incarnation(incarnation_id):
            return False
        with self._request_execution_transaction() as transaction:
            record = self._sessions.repository.get_by_incarnation(
                transaction, owner_id=owner_id, incarnation_id=incarnation_id)
        return bool(record is not None and record.session_id == session_id
                    and record.hard_expires_at > int(time.time()))

    @contextmanager
    def _request_execution_transaction(self):
        """Use Plane's request-only SQL caps; unwind failed SQL before refusal.

        Async cancellation cannot stop a running database thread. These local
        SQL caps release ordinary lock/query waits independently of that await;
        pool checkout and connection establishment retain their existing bounds.
        """
        try:
            with self._sessions.transaction() as transaction:
                self._sessions.repository.bound_request_execution_waits(transaction)
                yield transaction
        except Exception:
            raise SessionRefreshUnavailable("session execution database unavailable") from None

    def _refresh_record(self, sid, owner_id, reference, *, request_execution=False):
        scope = (self._request_execution_transaction() if request_execution
                 else self._sessions.transaction())
        with scope as transaction:
            if reference is None:
                record = self._sessions.repository.get(
                    transaction, owner_id=owner_id, session_id=sid)
            else:
                record = self._sessions.repository.get_by_incarnation(
                    transaction, owner_id=owner_id, incarnation_id=reference["incarnation_id"])
        if record is None or record.hard_expires_at <= int(time.time()):
            raise SessionRefreshUnavailable("session missing or expired")
        if reference is not None and (
            record.session_id != sid or record.incarnation_id != reference["incarnation_id"]
            or record.created_at != reference["created_at"]
            or record.interactive_anchor != reference["interactive_anchor"]
        ):
            raise SessionRefreshUnavailable("session identity changed; re-consent required")
        return record

    def _claim_refresh(self, sid, owner_id, reference, *, execution=None):
        if self._fernet is None:
            raise SessionRefreshUnavailable("encrypted session required for refresh")
        record = self._refresh_record(sid, owner_id, reference,
                                      request_execution=execution is not None)
        if execution is not None and SessionRepository.execution_fence(record) != execution.credential:
            raise SessionRefreshUnavailable("session changed before refresh")
        try:
            plaintext = self._fernet.decrypt(
                record.refresh_token_ciphertext.encode()).decode()
        except Exception:
            raise SessionRefreshUnavailable("session credential cannot be decrypted") from None
        if plaintext.startswith(_REFRESH_CLAIM_PREFIX):
            try:
                claim = json.loads(plaintext[len(_REFRESH_CLAIM_PREFIX):])
                started = claim["started"]
                if (type(started) not in (int, float) or
                        not 0 <= time.time() - started < REFRESH_WAIT_SECONDS):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise SessionRefreshUnavailable("prior refresh outcome unknown; sign in again") from None
            return None
        if not _valid_token(plaintext):
            raise SessionRefreshUnavailable("session has no valid refresh credential")
        marker = _REFRESH_CLAIM_PREFIX + json.dumps(
            {"nonce": secrets.token_hex(32), "started": time.time()})
        claimed = replace(record, refresh_token_ciphertext=self._enc(marker),
                          last_refresh_at=max(int(time.time()), record.last_refresh_at + 1))
        try:
            scope = (self._request_execution_transaction() if execution is not None
                     else self._sessions.transaction())
            with scope as transaction:
                if execution is not None:
                    self._sessions.repository.assert_current_execution(
                        transaction, observation=execution)
                self._sessions.repository.compare_and_set_refresh(
                    transaction, record=claimed,
                    expected_last_refresh_at=record.last_refresh_at,
                    expected_credential=SessionRepository.execution_fence(record))
        except RepositoryConflictError:
            return None
        except RepositoryNotFoundError:
            raise SessionRefreshUnavailable("session was revoked") from None
        return claimed, plaintext, self._dec(record.access_token_ciphertext)

    def _settle_refresh_record(self, claimed, access, refresh, reference, *, request_execution=False):
        self._refresh_record(claimed.session_id, claimed.owner_id, reference,
                             request_execution=request_execution)
        replacement = replace(
            claimed, access_token_ciphertext=self._enc(access),
            refresh_token_ciphertext=self._enc(refresh),
            last_refresh_at=max(int(time.time()), claimed.last_refresh_at + 1))
        try:
            scope = (self._request_execution_transaction() if request_execution
                     else self._sessions.transaction())
            with scope as transaction:
                replacement = self._sessions.repository.compare_and_set_refresh(
                    transaction, record=replacement,
                    expected_last_refresh_at=claimed.last_refresh_at,
                    expected_credential=SessionRepository.execution_fence(claimed))
        except (RepositoryConflictError, RepositoryNotFoundError):
            raise SessionRefreshUnavailable("session changed during refresh") from None
        if replacement.hard_expires_at <= int(time.time()):
            raise SessionRefreshUnavailable("session expired during refresh")
        row = self._from_record(replacement)
        self._remember_row(row)
        return replacement

    def _settle_refresh(self, claimed, access, refresh, reference):
        return self._from_record(self._settle_refresh_record(claimed, access, refresh, reference))

    def capture_execution_reference(self, *, owner_id: str, session_id: str) -> WebSessionReference:
        """Read exact owner/SID and DB time; never resolve a latest-owner session."""
        if self._fernet is None:
            raise SessionRefreshUnavailable("encrypted session required for execution")
        with self._request_execution_transaction() as transaction:
            state = self._sessions.repository.get_execution_state(
                transaction, owner_id=owner_id, session_id=session_id)
        if state is None:
            raise SessionRefreshUnavailable("session unavailable")
        return WebSessionReference(state)

    def capture_incarnation_execution_reference(
        self, *, owner_id: str, incarnation_id: str,
    ) -> WebSessionReference:
        """Resolve one issuance and capture its unchanged fence in one bounded read."""
        if self._fernet is None or not _valid_incarnation(incarnation_id):
            raise SessionRefreshUnavailable("encrypted issued session required")
        with self._request_execution_transaction() as transaction:
            record = self._sessions.repository.get_by_incarnation(
                transaction, owner_id=owner_id, incarnation_id=incarnation_id)
            if record is None or record.incarnation_id != incarnation_id:
                raise SessionRefreshUnavailable("issued session unavailable")
            state = self._sessions.repository.get_execution_state(
                transaction, owner_id=owner_id, session_id=record.session_id)
            if (state is None or state.credential.incarnation_id != incarnation_id
                    or state.credential != SessionRepository.execution_fence(record)):
                raise SessionRefreshUnavailable("issued session changed during capture")
        return WebSessionReference(state)

    async def refresh_for_execution(self, reference: WebSessionReference, *, exchange):
        """Force one exact-generation refresh, with no conflict/adoption retries.

        This candidate is not IAM authority. The host must verify its access token
        normally and check a bounded Plane observation before using it. An unknown
        remote outcome retains the existing durable claim and is never replayed.
        """
        if not isinstance(reference, WebSessionReference):
            raise SessionRefreshUnavailable("typed session reference required")
        state = reference.state
        if not isinstance(state, SessionExecutionState):
            raise SessionRefreshUnavailable("typed session state required")
        credential = state.credential
        execution = SessionExecutionObservation(
            credential=credential, started_at=state.observed_at,
            valid_until=min(state.observed_at + timedelta(seconds=15),
                            datetime.fromtimestamp(credential.hard_expires_at, timezone.utc)))
        try:
            async with asyncio.timeout(REFRESH_WAIT_SECONDS):
                acquired = await asyncio.to_thread(
                    self._claim_refresh, credential.session_id, credential.owner_id, None,
                    execution=execution)
                if acquired is None:
                    raise SessionRefreshUnavailable("session refresh already changed or claimed")
                claimed, refresh, access = acquired
                payload = await exchange(refresh, access)
                if not _refresh_payload_valid(payload):
                    raise SessionRefreshUnavailable("malformed refresh response")
                persisted = await asyncio.to_thread(
                    self._settle_refresh_record, claimed, payload["access_token"],
                    payload.get("refresh_token", refresh), None, request_execution=True)
                return RefreshedSessionCredential(
                    SessionRepository.execution_fence(persisted), state.observed_at,
                    payload["access_token"])
        except TimeoutError:
            raise SessionRefreshUnavailable("refresh time limit exceeded") from None

    def assert_execution_observation(self, observation: SessionExecutionObservation) -> None:
        """Validate after IAM; the mutation transaction must independently recheck."""
        with self._request_execution_transaction() as transaction:
            self._sessions.repository.assert_current_execution(
                transaction, observation=observation)

    async def refresh_credential(self, sid, *, owner_id, exchange, reference=None,
                                 expected_incarnation_id=None):
        """Serialize consumers before HTTP and persist rotation before returning.

        Cancellation, a crash, or an ambiguous response keeps the authenticated
        claim in place. Nobody retries the potentially consumed old token.
        """
        try:
            async with asyncio.timeout(REFRESH_WAIT_SECONDS):
                if reference is not None:
                    if (not isinstance(reference, dict)
                            or set(reference) != {"session_id", "incarnation_id", "created_at", "interactive_anchor"}
                            or reference["session_id"] != sid
                            or not _valid_incarnation(reference["incarnation_id"])
                            or any(type(reference[k]) is not int for k in ("created_at", "interactive_anchor"))):
                        raise SessionRefreshUnavailable("issued session reference required")
                    reference = dict(reference)
                if expected_incarnation_id is not None and not _valid_incarnation(expected_incarnation_id):
                    raise SessionRefreshUnavailable("issued session identity required")
                initial = await asyncio.to_thread(self._refresh_record, sid, owner_id, reference)
                if expected_incarnation_id is not None and initial.incarnation_id != expected_incarnation_id:
                    raise SessionRefreshUnavailable("session identity changed")
                # Freeze before the first claim/retry/HTTP await. A replacement
                # with identical SID, time and ciphertext cannot be adopted.
                reference = {"session_id": sid, "incarnation_id": initial.incarnation_id,
                             "created_at": initial.created_at,
                             "interactive_anchor": initial.interactive_anchor}
                while True:
                    acquired = await asyncio.to_thread(
                        self._claim_refresh, sid, owner_id, reference)
                    if acquired is not None:
                        break
                    await asyncio.sleep(.1)
                claimed, refresh, access = acquired
                payload = await exchange(refresh, access)
                if not _refresh_payload_valid(payload):
                    raise SessionRefreshUnavailable("malformed refresh response")
                return await asyncio.to_thread(
                    self._settle_refresh, claimed, payload["access_token"],
                    payload.get("refresh_token", refresh), reference)
        except TimeoutError:
            raise SessionRefreshUnavailable("refresh time limit exceeded") from None

    def pop_death_reason(self, sid: str) -> Optional[str]:
        """Why get() last refused this sid ('hard_cap'), consumed on read."""
        return self._death_reasons.pop(sid, None)

    def update_tokens(self, sid: str, *, access_token: str, refresh_token: str,
                      expected_incarnation_id: str | None = None) -> None:
        """Rotate tokens after a silent refresh. NEVER moves the anchor (016 FR-001)."""
        observed = self._cache.get(sid)
        with self._sessions.transaction() as transaction:
            current = self._sessions.repository.get_by_session_id_for_administration(
                transaction,
                session_id=sid,
            )
            if current is None:
                return
            if expected_incarnation_id is not None and current.incarnation_id != expected_incarnation_id:
                raise SessionRefreshUnavailable("session identity changed")
            if not self._dec(current.refresh_token_ciphertext):
                raise SessionRefreshUnavailable("unsettled refresh cannot be overwritten")
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
                incarnation_id=current.incarnation_id,
            )
            stored = self._sessions.repository.compare_and_set_refresh(
                transaction,
                refreshed,
                expected_last_refresh_at=current.last_refresh_at,
            )
        self._remember_row(self._from_record(stored), observed)

    def mark_resumed(self, sid: str, resumed: bool = True, *,
                     expected_incarnation_id: str | None = None) -> None:
        """Mark only the originally observed issuance as silently resumed."""
        observed = self._cache.get(sid)
        target = bool(resumed)
        with self._sessions.transaction() as transaction:
            current = self._sessions.repository.get_by_session_id_for_administration(
                transaction,
                session_id=sid,
            )
            if current is None:
                return
            if expected_incarnation_id is not None and current.incarnation_id != expected_incarnation_id:
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
                    expected_incarnation_id=current.incarnation_id,
                )
            )
        self._remember_row(self._from_record(stored), observed)

    def delete(self, sid: str, *, expected_incarnation_id: str | None = None) -> Optional[Dict[str, Any]]:
        """Delete and return the exact durable credential for revocation."""
        observed = self._cache.get(sid)
        with self._sessions.transaction() as transaction:
            record = self._sessions.repository.get_by_session_id_for_administration(
                transaction,
                session_id=sid,
            )
            if record is None:
                self._evict_cached(sid, observed=observed)
                return None
            if expected_incarnation_id is not None and record.incarnation_id != expected_incarnation_id:
                self._evict_cached(sid, incarnation_id=expected_incarnation_id)
                return None
            deleted = self._sessions.repository.delete_and_return(
                transaction,
                owner_id=record.owner_id,
                session_id=record.session_id,
                expected_incarnation_id=record.incarnation_id,
            )
        self._evict_cached(sid, incarnation_id=record.incarnation_id)
        return None if deleted is None else self._from_record(deleted)

    def delete_for_user(self, user_id: str) -> int:
        """Delete every session of a user (user-switch revocation, 016 FR-008)."""
        with self._cache_lock:
            for sid in [s for s, r in self._cache.items() if r.get("user_id") == user_id]:
                self._cache.pop(sid, None)
        return self._sessions.call(
            self._sessions.repository.delete_owner,
            owner_id=user_id,
        )

    def purge_expired(self) -> int:
        """Opportunistic cleanup of hard-cap-expired rows."""
        now = int(time.time())
        with self._cache_lock:
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

    async def aupdate_tokens(self, sid: str, *, access_token: str, refresh_token: str,
                            expected_incarnation_id: str | None = None) -> None:
        """Async twin of :meth:`update_tokens`, run off the event loop."""
        return await asyncio.to_thread(
            self.update_tokens, sid, access_token=access_token, refresh_token=refresh_token,
            expected_incarnation_id=expected_incarnation_id,
        )

    async def amark_resumed(self, sid: str, resumed: bool = True, *,
                           expected_incarnation_id: str | None = None) -> None:
        """Async twin of :meth:`mark_resumed`, run off the event loop."""
        return await asyncio.to_thread(self.mark_resumed, sid, resumed,
                                       expected_incarnation_id=expected_incarnation_id)

    async def adelete(self, sid: str, *, expected_incarnation_id: str | None = None) -> Optional[Dict[str, Any]]:
        """Async twin of :meth:`delete`, run off the event loop."""
        return await asyncio.to_thread(self.delete, sid,
                                       expected_incarnation_id=expected_incarnation_id)

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
