"""Persisted per-user + deployment-system LLM configuration store
(feature 054-byo-llm-setup).

Replaces feature 006's per-WebSocket in-memory ``SessionCredentialStore``:
configuration done once on any client applies to all of the user's clients
and sessions, survives disconnect and sign-out, and is resolvable by
``user_id`` — which is what makes the mandatory first-run gate, the watch,
and scheduled-job turns possible at all.

Storage: two Plane-owned records exposed by the typed secrets repository:

* ``user_llm_config`` — one row per configured user (PK ``user_id``).
* ``system_llm_config`` — zero-or-one admin-managed row (PK CHECK id=1),
  used EXCLUSIVELY for system-context calls (scheduled jobs, codegen,
  knowledge synthesis, compaction, workspace combine/condense, narration).
  Never serves user chat, and user records never serve system calls.

Security posture (spec FR-006/FR-007, carried over from 006):

* ``api_key`` is Fernet-encrypted at rest under ``CREDENTIAL_ENCRYPTION_KEY``
  (same key + dev key-file fallback as the agent credential store; the key
  is production-boot-gated by ``assert_production_posture``).
* The plaintext key never appears in logs (``__repr__`` elides it), audit
  payloads (``_assert_no_api_key``), or client-bound payloads (surfaces
  receive only ``has_key``).
* An undecryptable row (key rotation, corruption) is treated as ABSENT:
  audited, deleted, and the user is re-gated — never a crash (FR-010).

Concurrency: repository reads/writes are synchronous caller-owned Plane
transactions; the async wrappers run them via ``asyncio.to_thread`` so the
event loop is never blocked (feature 052 loop-guard). A small in-process TTL
cache fronts the reads; ``set``/``clear`` invalidate synchronously, so gate
transitions are immediate within the process.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Dict, Optional

from astralplane.repositories import RepositoryNotFoundError
from astralplane.repositories.secrets import EncryptedLLMConfigRecord
from cryptography.fernet import Fernet, InvalidToken
from orchestrator.plane_repository_context import PlaneRepositoryContext, repository_from
from orchestrator.work_admission import OperationState

logger = logging.getLogger("LLMConfig.UserStore")

# Cache TTL for read-through lookups. Set/clear invalidate synchronously in
# this process; the TTL only bounds staleness across processes (single-
# process deployments never observe it).
_CACHE_TTL_SECONDS = 30.0

_SYSTEM_CACHE_KEY = "__system__"


class LLMConfigCommitDeadlineExceeded(TimeoutError):
    """The credential write reached its commit fence after the attempt bound."""


@dataclass(slots=True)
class FencedLLMConfigCommit:
    """Credential row and durable COMPLETED winner from one transaction."""

    config: "PersistedLLMConfig"
    operation: Any


@dataclass(slots=True)
class PersistedLLMConfig:
    """A decrypted, usable LLM provider configuration.

    The working shape handed to ``client_factory.build_llm_client``. The
    ``api_key`` may be ``""`` for keyless local-runtime presets.
    """
    provider: str
    base_url: str
    model: str
    api_key: str
    updated_at: Optional[float] = None

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    def __repr__(self) -> str:
        # Elide api_key — same posture as 006's SessionCreds.__repr__.
        return (
            f"PersistedLLMConfig(provider={self.provider!r}, "
            f"base_url={self.base_url!r}, model={self.model!r}, "
            f"api_key=<redacted>)"
        )


def _resolve_fernet(data_dir: Optional[str] = None) -> Fernet:
    """Resolve the at-rest encryption key.

    Same resolution as ``orchestrator.credential_manager``: the
    ``CREDENTIAL_ENCRYPTION_KEY`` env var in production (boot-gated), with
    the auto-generated ``backend/data/.credential_key`` file as the
    development fallback — so both stores decrypt with one key.
    """
    env_key = os.getenv("CREDENTIAL_ENCRYPTION_KEY")
    if env_key:
        return Fernet(env_key.encode())
    key_dir = data_dir or os.path.join(os.path.dirname(__file__), "..", "data")
    key_path = os.path.join(key_dir, ".credential_key")
    if os.path.exists(key_path):
        with open(key_path, "rb") as f:
            key = f.read().strip()
    else:
        key = Fernet.generate_key()
        os.makedirs(os.path.dirname(key_path), exist_ok=True)
        with open(key_path, "wb") as f:
            f.write(key)
        logger.info("Generated new credential encryption key (dev fallback)")
    return Fernet(key)


class UserLLMConfigStore:
    """DB-backed store for per-user and system LLM configuration."""

    def __init__(
        self,
        db=None,
        *,
        data_dir: Optional[str] = None,
        plane_runtime=None,
        plane_repositories=None,
        encrypted_llm_config_repository=None,
    ) -> None:
        self.db = db
        repository, runtime = repository_from(
            "encrypted_llm_config",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=db,
        )
        self._repository = PlaneRepositoryContext(
            repository=encrypted_llm_config_repository or repository,
            plane_runtime=runtime,
            legacy_database=db,
        )
        self._fernet = _resolve_fernet(data_dir)
        # cache key -> (expires_monotonic, PersistedLLMConfig | None)
        self._cache: Dict[str, tuple] = {}

    # ------------------------------------------------------------------
    # Cache plumbing
    # ------------------------------------------------------------------

    def _cache_get(self, key: str):
        entry = self._cache.get(key)
        if entry is None:
            return False, None
        expires, value = entry
        if time.monotonic() > expires:
            self._cache.pop(key, None)
            return False, None
        return True, value

    def _cache_put(self, key: str, value: Optional[PersistedLLMConfig]) -> None:
        self._cache[key] = (time.monotonic() + _CACHE_TTL_SECONDS, value)

    def invalidate(self, user_id: str) -> None:
        """Drop the cached entry for ``user_id`` (or the system row)."""
        self._cache.pop(user_id, None)

    # ------------------------------------------------------------------
    # Crypto
    # ------------------------------------------------------------------

    def _encrypt_key(self, api_key: str) -> Optional[str]:
        if not api_key:
            return None
        return self._fernet.encrypt(api_key.encode()).decode()

    def _decrypt_key(self, api_key_enc: Optional[str]) -> str:
        """Decrypt, raising :class:`InvalidToken` on an unusable ciphertext."""
        if not api_key_enc:
            return ""
        return self._fernet.decrypt(api_key_enc.encode()).decode()

    # ------------------------------------------------------------------
    # Per-user record (sync core — call via the async wrappers on the loop)
    # ------------------------------------------------------------------

    def get_sync(self, user_id: str) -> Optional[PersistedLLMConfig]:
        """Return the user's decrypted configuration, or ``None``.

        An undecryptable row is audited by the caller's audit hook (see
        :meth:`pop_discard_note`), deleted here, and reported as absent —
        the FR-010 "treated as not configured" path.
        """
        hit, value = self._cache_get(user_id)
        if hit:
            return value
        row = self._repository.call(
            self._repository.repository.get_user,
            owner_id=user_id,
        )
        value = self._row_to_config(row, discard_scope="user", discard_id=user_id)
        self._cache_put(user_id, value)
        return value

    def set_sync(self, user_id: str, *, provider: str, base_url: str,
                 model: str, api_key: str) -> PersistedLLMConfig:
        """Persist (upsert) the user's configuration. Field validation is
        the caller's job (ws_handlers validates + probes before persisting);
        this method only enforces non-empty structural fields."""
        provider = (provider or "").strip() or "custom"
        base_url = (base_url or "").strip().rstrip("/")
        model = (model or "").strip()
        api_key = (api_key or "").strip()
        if not base_url or not model:
            raise ValueError("base_url and model must be non-empty")
        with self._repository.transaction() as transaction:
            record = self._repository.repository.upsert_user(
                transaction,
                owner_id=user_id,
                provider=provider,
                base_url=base_url,
                model=model,
                api_key_ciphertext=self._encrypt_key(api_key),
            )
        cfg = PersistedLLMConfig(
            provider=provider,
            base_url=base_url,
            model=model,
            api_key=api_key,
            updated_at=record.updated_at.timestamp(),
        )
        self._cache_put(user_id, cfg)
        return cfg

    def set_fenced_sync(
        self,
        user_id: str,
        *,
        provider: str,
        base_url: str,
        model: str,
        api_key: str,
        coordinator: Any,
        fence: Any,
        deadline_at_monotonic: float,
        deadline_at_utc: datetime,
    ) -> FencedLLMConfigCommit:
        """Commit a credential update only for the current live execution.

        Production writes use the PostgreSQL cursor yielded by the operation
        coordinator, so the full generation/token check and the credential
        upsert share one transaction.  The final DML statement also checks
        ``clock_timestamp()`` against the attempt deadline; a provider worker
        that returns after its coroutine was cancelled therefore cannot
        publish a late credential.  The in-memory repository keeps the same
        ordering under its lock for deterministic contract tests.
        """

        provider = (provider or "").strip() or "custom"
        base_url = (base_url or "").strip().rstrip("/")
        model = (model or "").strip()
        api_key = (api_key or "").strip()
        if not base_url or not model:
            raise ValueError("base_url and model must be non-empty")
        if time.monotonic() >= deadline_at_monotonic:
            raise LLMConfigCommitDeadlineExceeded(
                "credential save deadline elapsed before persistence"
            )
        if deadline_at_utc.tzinfo is None:
            deadline_at_utc = deadline_at_utc.replace(tzinfo=UTC)
        deadline_at_utc = deadline_at_utc.astimezone(UTC)
        encrypted_key = self._encrypt_key(api_key)

        terminal = None
        with coordinator.fenced_transaction(fence) as transaction:
            if time.monotonic() >= deadline_at_monotonic:
                raise LLMConfigCommitDeadlineExceeded(
                    "credential save deadline elapsed before persistence"
                )
            if callable(getattr(transaction, "fetch_one", None)):
                record = self._repository.repository.upsert_user_before_deadline(
                    transaction,
                    owner_id=user_id,
                    provider=provider,
                    base_url=base_url,
                    model=model,
                    api_key_ciphertext=encrypted_key,
                    deadline_at=deadline_at_utc,
                )
                if record is None:
                    raise LLMConfigCommitDeadlineExceeded(
                        "credential save deadline elapsed before persistence"
                    )
                if (
                    time.monotonic() >= deadline_at_monotonic
                    or record.updated_at >= deadline_at_utc
                ):
                    raise LLMConfigCommitDeadlineExceeded(
                        "credential save deadline elapsed before completion"
                    )
                terminal = coordinator.terminalize(
                    fence,
                    state=OperationState.COMPLETED,
                    terminal_code=None,
                    safe_summary="Completed",
                    retry_after_ms=None,
                    transaction=transaction,
                )
            else:
                # ``fenced_transaction`` already holds the in-memory
                # repository lock and asserted the complete execution fence.
                # Complete first while the same lock is held, then publish the
                # FakeDB effect. This models the production transaction's
                # indivisible winner without requiring a second test DB API.
                if time.monotonic() >= deadline_at_monotonic:
                    raise LLMConfigCommitDeadlineExceeded(
                        "credential save deadline elapsed before completion"
                    )
                terminal = coordinator.terminalize(
                    fence,
                    state=OperationState.COMPLETED,
                    terminal_code=None,
                    safe_summary="Completed",
                    retry_after_ms=None,
                    transaction=transaction,
                )
                self._repository.repository.upsert_user(
                    transaction,
                    owner_id=user_id,
                    provider=provider,
                    base_url=base_url,
                    model=model,
                    api_key_ciphertext=encrypted_key,
                )

        cfg = PersistedLLMConfig(
            provider=provider,
            base_url=base_url,
            model=model,
            api_key=api_key,
            updated_at=time.time(),
        )
        self._cache_put(user_id, cfg)
        return FencedLLMConfigCommit(config=cfg, operation=terminal)

    def clear_sync(self, user_id: str) -> bool:
        """Delete the user's configuration. Returns True iff a row existed."""
        row = self._repository.call(
            self._repository.repository.get_user,
            owner_id=user_id,
        )
        if row is not None:
            with self._repository.transaction() as transaction:
                self._repository.repository.delete_user(
                    transaction,
                    owner_id=user_id,
                )
        self.invalidate(user_id)
        self._cache_put(user_id, None)
        return row is not None

    # ------------------------------------------------------------------
    # System record (admin-managed; system-context calls only)
    # ------------------------------------------------------------------

    def get_system_sync(self) -> Optional[PersistedLLMConfig]:
        hit, value = self._cache_get(_SYSTEM_CACHE_KEY)
        if hit:
            return value
        row = self._repository.call(
            self._repository.repository.get_system,
        )
        value = self._row_to_config(row, discard_scope="system", discard_id=_SYSTEM_CACHE_KEY)
        self._cache_put(_SYSTEM_CACHE_KEY, value)
        return value

    def set_system_sync(self, *, provider: str, base_url: str, model: str,
                        api_key: str, updated_by: str) -> PersistedLLMConfig:
        provider = (provider or "").strip() or "custom"
        base_url = (base_url or "").strip().rstrip("/")
        model = (model or "").strip()
        api_key = (api_key or "").strip()
        if not base_url or not model:
            raise ValueError("base_url and model must be non-empty")
        with self._repository.transaction() as transaction:
            record = self._repository.repository.upsert_system(
                transaction,
                updated_by=updated_by,
                provider=provider,
                base_url=base_url,
                model=model,
                api_key_ciphertext=self._encrypt_key(api_key),
            )
        cfg = PersistedLLMConfig(
            provider=provider,
            base_url=base_url,
            model=model,
            api_key=api_key,
            updated_at=record.updated_at.timestamp(),
        )
        self._cache_put(_SYSTEM_CACHE_KEY, cfg)
        return cfg

    def clear_system_sync(self) -> bool:
        row = self._repository.call(self._repository.repository.get_system)
        if row is not None:
            with self._repository.transaction() as transaction:
                self._repository.repository.delete_system(transaction)
        self._cache_put(_SYSTEM_CACHE_KEY, None)
        return row is not None

    # ------------------------------------------------------------------
    # Async wrappers (event-loop-safe; feature 052 loop-guard)
    # ------------------------------------------------------------------

    async def get(self, user_id: str) -> Optional[PersistedLLMConfig]:
        hit, value = self._cache_get(user_id)
        if hit:
            return value
        return await asyncio.to_thread(self.get_sync, user_id)

    async def set(self, user_id: str, *, provider: str, base_url: str,
                  model: str, api_key: str) -> PersistedLLMConfig:
        return await asyncio.to_thread(
            self.set_sync, user_id, provider=provider, base_url=base_url,
            model=model, api_key=api_key)

    async def set_fenced(
        self,
        user_id: str,
        *,
        provider: str,
        base_url: str,
        model: str,
        api_key: str,
        coordinator: Any,
        fence: Any,
        deadline_at_monotonic: float,
        deadline_at_utc: datetime,
    ) -> FencedLLMConfigCommit:
        return await asyncio.to_thread(
            self.set_fenced_sync,
            user_id,
            provider=provider,
            base_url=base_url,
            model=model,
            api_key=api_key,
            coordinator=coordinator,
            fence=fence,
            deadline_at_monotonic=deadline_at_monotonic,
            deadline_at_utc=deadline_at_utc,
        )

    async def clear(self, user_id: str) -> bool:
        return await asyncio.to_thread(self.clear_sync, user_id)

    async def get_system(self) -> Optional[PersistedLLMConfig]:
        hit, value = self._cache_get(_SYSTEM_CACHE_KEY)
        if hit:
            return value
        return await asyncio.to_thread(self.get_system_sync)

    async def set_system(self, *, provider: str, base_url: str, model: str,
                         api_key: str, updated_by: str) -> PersistedLLMConfig:
        return await asyncio.to_thread(
            self.set_system_sync, provider=provider, base_url=base_url,
            model=model, api_key=api_key, updated_by=updated_by)

    async def clear_system(self) -> bool:
        return await asyncio.to_thread(self.clear_system_sync)

    # ------------------------------------------------------------------
    # Shared row handling
    # ------------------------------------------------------------------

    def _row_to_config(self, row: Optional[EncryptedLLMConfigRecord], *, discard_scope: str,
                       discard_id: str) -> Optional[PersistedLLMConfig]:
        if row is None:
            return None
        try:
            api_key = self._decrypt_key(row.api_key_ciphertext)
        except (InvalidToken, ValueError, TypeError):
            # FR-010: undecryptable ⇒ discard + treat as absent. The deletion
            # is immediate; the audit note is queued for the orchestrator's
            # async audit hook (a sync store cannot await the recorder).
            logger.warning(
                "Discarding undecryptable %s LLM config record (key rotation "
                "or corruption); treated as unconfigured", discard_scope)
            try:
                if discard_scope == "system":
                    with self._repository.transaction() as transaction:
                        self._repository.repository.delete_system(transaction)
                else:
                    with self._repository.transaction() as transaction:
                        self._repository.repository.delete_user(
                            transaction,
                            owner_id=discard_id,
                        )
            except RepositoryNotFoundError:
                pass
            except Exception:  # pragma: no cover — deletion is best-effort
                logger.exception("Failed to delete undecryptable LLM config row")
            if not hasattr(self, "_pending_discards"):
                self._pending_discards = []
            self._pending_discards.append((discard_scope, discard_id))
            return None
        return PersistedLLMConfig(
            provider=row.provider or "custom",
            base_url=(row.base_url or "").rstrip("/"),
            model=row.model or "",
            api_key=api_key,
            updated_at=row.updated_at.timestamp(),
        )

    def pop_discard_note(self) -> Optional[tuple]:
        """Return one queued (scope, id) undecryptable-discard note, or None.

        The orchestrator drains these after resolution attempts and emits the
        ``llm_config_change{action:"discarded_undecryptable"}`` audit event.
        """
        pending = getattr(self, "_pending_discards", None)
        if pending:
            return pending.pop(0)
        return None
