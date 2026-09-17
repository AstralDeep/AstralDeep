"""Persisted per-user TypeSafe credential store (feature 089).

The counterpart to :mod:`llm_config.user_store`, deliberately much smaller.
A TypeSafe credential is one opaque key plus the outcome of the last time the
product used it. There is no provider, no base URL and no model to persist:
those are code constants, because 089 refuses environment-supplied TypeSafe
configuration outright (FR-005).

Storage is the Plane ``user_typesafe_credential`` table at schema revision
``089.001``, reached through the typed
``EncryptedTypeSafeCredentialRepository``. Plane sees ciphertext only.

Security posture:

* The key is Fernet-encrypted at rest under the **same**
  ``CREDENTIAL_ENCRYPTION_KEY`` the LLM store uses, resolved by
  :func:`llm_config.user_store._resolve_fernet`, so one key rotation covers
  both stores and a deployment cannot end up with two crypto roots.
* :class:`TypeSafeKeyStatus` is what surfaces render. It carries no key and no
  ciphertext -- only ``not_set`` / ``active`` / ``rejected`` / ``unavailable``
  and a timestamp.
* ``key_fingerprint`` is ``sha256(key)[:12]``. It never reaches a client, a log
  line beside a user identifier, or an audit payload. Its only job is to make
  :meth:`record_outcome_async` idempotent against key replacement.
* An undecryptable row is discarded and the user simply has no TypeSafe key,
  mirroring the LLM store's FR-010 behavior. A discard note is queued for the
  orchestrator's async audit hook, because a synchronous store cannot await a
  recorder.

Isolation from the LLM gate is a hard rule (FR-004). Nothing in this module
participates in ``llm_configured_for``, first-run gating, or unlock/regate.
A user with a TypeSafe key and no LLM configuration stays gated; a user who
clears their TypeSafe key is not re-gated.

Concurrency follows the LLM store: synchronous repository calls inside
caller-owned Plane transactions, wrapped for the event loop with
``asyncio.to_thread``. A short TTL cache fronts reads and is invalidated
synchronously on save and clear, so a settings round trip is immediately
consistent within the process.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import logging
import time
from typing import Any, Literal, Optional

from astralplane.repositories import (
    RepositoryError,
    RepositoryNotFoundError,
)
from cryptography.fernet import InvalidToken
from orchestrator.plane_repository_context import PlaneRepositoryContext, repository_from

from .user_store import _resolve_fernet

logger = logging.getLogger("LLMConfig.TypeSafeStore")

# Same bound as the LLM store: the TTL only limits cross-process staleness,
# because save and clear invalidate this process synchronously.
_CACHE_TTL_SECONDS = 30.0

#: The longest key the store will accept. A TypeSafe key is far shorter; this
#: exists so a paste accident cannot push an arbitrarily large blob through
#: encryption and into the database.
MAX_KEY_CHARS = 512

Outcome = Literal["valid", "rejected", "unavailable"]
StatusName = Literal["not_set", "active", "rejected", "unavailable"]


def key_fingerprint(api_key: str) -> str:
    """Return the 12-hex-character fingerprint stored beside a key.

    Truncating to 12 characters is deliberate. The fingerprint only has to
    distinguish "the key I just observed an outcome on" from "the key that is
    stored now"; it is never an authentication factor, and a shorter value is
    less useful to anyone who does get hold of it.
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class TypeSafeKeyStatus:
    """What a settings surface is allowed to know about a stored key.

    Carries no key material and no ciphertext, so it is safe to serialize into
    an SDUI component or a rendered page.
    """

    name: StatusName = "not_set"
    at: Optional[datetime] = None
    last_verified_at: Optional[datetime] = None

    @property
    def is_set(self) -> bool:
        return self.name != "not_set"

    @property
    def is_usable(self) -> bool:
        """True when the product should attempt routing with this key.

        A rejected key is not retried until the user saves a new one. An
        unavailable key is still attempted: unavailable describes TypeSafe, not
        the credential, and the circuit breaker -- not this flag -- is what
        stops the retry storm.
        """
        return self.name in ("active", "unavailable")


@dataclass(frozen=True, slots=True)
class StoredTypeSafeKey:
    """A decrypted key with the fingerprint the outcome must be recorded against."""

    api_key: str = field(repr=False)
    fingerprint: str

    def __repr__(self) -> str:  # pragma: no cover - trivial, but load-bearing
        return f"StoredTypeSafeKey(fingerprint={self.fingerprint!r})"


class TypeSafeCredentialStore:
    """Owner-scoped TypeSafe credential facade over the Plane repository."""

    def __init__(
        self,
        db: Any = None,
        *,
        data_dir: Optional[str] = None,
        plane_runtime: Any = None,
        plane_repositories: Any = None,
        typesafe_credential_repository: Any = None,
    ) -> None:
        self.db = db
        repository, runtime = repository_from(
            "encrypted_typesafe_credential",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=db,
        )
        self._repository = PlaneRepositoryContext(
            repository=typesafe_credential_repository or repository,
            plane_runtime=runtime,
            legacy_database=db,
        )
        self._fernet = _resolve_fernet(data_dir)
        self._cache: dict[str, tuple[float, Any]] = {}
        self._pending_discards: list[str] = []

    # -- cache ----------------------------------------------------------

    def _cache_get(self, user_id: str) -> tuple[bool, Any]:
        entry = self._cache.get(user_id)
        if entry is None:
            return False, None
        expires, value = entry
        if time.monotonic() > expires:
            self._cache.pop(user_id, None)
            return False, None
        return True, value

    def _cache_put(self, user_id: str, value: Any) -> None:
        self._cache[user_id] = (time.monotonic() + _CACHE_TTL_SECONDS, value)

    def invalidate(self, user_id: str) -> None:
        """Drop the cached row for ``user_id``."""
        self._cache.pop(user_id, None)

    # -- crypto ---------------------------------------------------------

    def _encrypt(self, api_key: str) -> str:
        return self._fernet.encrypt(api_key.encode("utf-8")).decode("ascii")

    def _decrypt(self, ciphertext: str) -> str:
        return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")

    # -- reads ----------------------------------------------------------

    def _row_sync(self, user_id: str) -> Any:
        hit, value = self._cache_get(user_id)
        if hit:
            return value
        try:
            row = self._repository.call(
                self._repository.repository.get_user,
                owner_id=user_id,
            )
        except RepositoryError:
            # A durable read failure must not break a turn. The user is treated
            # as having no key for this attempt and standard routing runs.
            logger.warning("TypeSafe credential read failed; treating as unset", exc_info=True)
            return None
        self._cache_put(user_id, row)
        return row

    def get_key_sync(self, user_id: str) -> Optional[StoredTypeSafeKey]:
        """Return the decrypted key and its fingerprint, or ``None``.

        An undecryptable row -- a rotated encryption key, or corruption -- is
        deleted and reported as absent rather than raised, so a broken row can
        never take a turn down with it.
        """
        row = self._row_sync(user_id)
        if row is None:
            return None
        try:
            api_key = self._decrypt(row.api_key_ciphertext)
        except (InvalidToken, ValueError, TypeError, UnicodeError):
            self._discard_undecryptable(user_id)
            return None
        if not api_key:
            return None
        return StoredTypeSafeKey(api_key=api_key, fingerprint=row.key_fingerprint)

    async def get_key(self, user_id: str) -> Optional[StoredTypeSafeKey]:
        return await asyncio.to_thread(self.get_key_sync, user_id)

    def status_sync(self, user_id: str) -> TypeSafeKeyStatus:
        """Return the renderable status for ``user_id``."""
        row = self._row_sync(user_id)
        if row is None:
            return TypeSafeKeyStatus()
        outcome = row.last_verification_outcome
        name: StatusName = "active"
        if outcome == "rejected":
            name = "rejected"
        elif outcome == "unavailable":
            name = "unavailable"
        return TypeSafeKeyStatus(
            name=name,
            at=row.last_outcome_at,
            last_verified_at=row.last_verified_at,
        )

    async def status(self, user_id: str) -> TypeSafeKeyStatus:
        return await asyncio.to_thread(self.status_sync, user_id)

    # -- writes ---------------------------------------------------------

    def save_sync(self, user_id: str, api_key: str) -> TypeSafeKeyStatus:
        """Persist ``api_key`` for ``user_id`` after a successful probe.

        The caller probes first. This method is the durable half only: it does
        not validate the key against TypeSafe, because a save that reached here
        has already been proven usable, and a save that has not must never
        overwrite a working stored key (FR-003).
        """
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("a TypeSafe key must be a non-empty string")
        api_key = api_key.strip()
        if len(api_key) > MAX_KEY_CHARS:
            raise ValueError("the TypeSafe key exceeds the maximum accepted length")
        fingerprint = key_fingerprint(api_key)
        verified_at = datetime.now(UTC)
        self._repository.call(
            self._repository.repository.upsert_user,
            owner_id=user_id,
            api_key_ciphertext=self._encrypt(api_key),
            key_fingerprint=fingerprint,
            verified_at=verified_at,
        )
        self.invalidate(user_id)
        return TypeSafeKeyStatus(
            name="active", at=verified_at, last_verified_at=verified_at
        )

    async def save(self, user_id: str, api_key: str) -> TypeSafeKeyStatus:
        return await asyncio.to_thread(self.save_sync, user_id, api_key)

    def clear_sync(self, user_id: str) -> bool:
        """Remove the user's key. Returns False when there was nothing to remove.

        Clearing is idempotent by design: a second Remove on a settings page
        that is already up to date is a no-op, not an error.
        """
        try:
            removed = bool(
                self._repository.call(
                    self._repository.repository.delete_user,
                    owner_id=user_id,
                )
            )
        except RepositoryNotFoundError:
            removed = False
        finally:
            self.invalidate(user_id)
        return removed

    async def clear(self, user_id: str) -> bool:
        return await asyncio.to_thread(self.clear_sync, user_id)

    def record_outcome_sync(
        self,
        user_id: str,
        outcome: Outcome,
        fingerprint: str,
    ) -> bool:
        """Record a turn's verdict on the key it was actually observed on.

        Returns False when nothing was updated, which is the normal result when
        the user replaced their key while this outcome was in flight. Every
        failure here is swallowed: an outcome is bookkeeping, and losing one
        must never surface to the user or disturb a turn.
        """
        try:
            applied = bool(
                self._repository.call(
                    self._repository.repository.record_outcome,
                    owner_id=user_id,
                    outcome=outcome,
                    at=datetime.now(UTC),
                    expected_fingerprint=fingerprint,
                )
            )
        except Exception:  # pragma: no cover - bookkeeping never breaks a turn
            logger.debug("TypeSafe outcome recording failed (non-fatal)", exc_info=True)
            return False
        if applied:
            self.invalidate(user_id)
        return applied

    async def record_outcome_async(
        self,
        user_id: str,
        outcome: Outcome,
        fingerprint: str,
    ) -> bool:
        """Record an outcome off the hot path.

        A turn schedules this and never awaits it. It is deliberately not a
        bare ``create_task`` here: the caller owns task lifetime so a cancelled
        turn does not leave an orphan.
        """
        return await asyncio.to_thread(
            self.record_outcome_sync, user_id, outcome, fingerprint
        )

    # -- discard --------------------------------------------------------

    def _discard_undecryptable(self, user_id: str) -> None:
        """Delete a row this process cannot decrypt and queue an audit note."""
        logger.warning(
            "Discarding undecryptable TypeSafe credential row (key rotation or "
            "corruption); the user is treated as having no TypeSafe key"
        )
        try:
            self._repository.call(
                self._repository.repository.delete_user,
                owner_id=user_id,
            )
        except RepositoryNotFoundError:
            pass
        except Exception:  # pragma: no cover - deletion is best-effort
            logger.exception("Failed to delete undecryptable TypeSafe credential row")
        self.invalidate(user_id)
        self._pending_discards.append(user_id)

    def pop_discard_note(self) -> Optional[str]:
        """Return one queued undecryptable-discard user id, or ``None``.

        The orchestrator drains these and emits
        ``typesafe_credential.discarded``, mirroring how the LLM store's
        discards are audited.
        """
        if self._pending_discards:
            return self._pending_discards.pop(0)
        return None


__all__ = (
    "MAX_KEY_CHARS",
    "Outcome",
    "StatusName",
    "StoredTypeSafeKey",
    "TypeSafeCredentialStore",
    "TypeSafeKeyStatus",
    "key_fingerprint",
)
