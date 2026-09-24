"""Persisted per-user and admin-managed system LLM configuration store, encrypted at
rest. System rows serve only system-context calls (codegen, scheduled jobs) and never
user chat, or vice versa. Backs ws_handlers.py and client_factory.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Dict, Optional

from astralplane.repositories import RepositoryNotFoundError
from astralplane.repositories.secrets import EncryptedLLMConfigRecord
from cryptography.fernet import Fernet, InvalidToken
from orchestrator.plane_repository_context import PlaneRepositoryContext, repository_from
from orchestrator.work_admission import OperationState

logger = logging.getLogger("LLMConfig.UserStore")

_CACHE_TTL_SECONDS = 30.0

_SYSTEM_CACHE_KEY = "__system__"


class UserConfigCaptureUnavailable(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CapturedUserLLMConfig:
    _record: EncryptedLLMConfigRecord = field(repr=False)

    @property
    def owner_id(self) -> str:
        return self._record.owner_id

    def matches(self, record: EncryptedLLMConfigRecord | None) -> bool:
        return type(record) is EncryptedLLMConfigRecord and self._record == record


def _capture_user_row(row, owner_id):
    try:
        if type(owner_id) is not str or not owner_id.strip() or len(owner_id.encode()) > 2048:
            raise ValueError
        if row is None:
            return None
        if (
            type(row) is not EncryptedLLMConfigRecord or row.scope != "user"
            or row.owner_id != owner_id or row.updated_by is not None
        ):
            raise ValueError
        for value in (row.provider, row.base_url, row.model, row.api_key_ciphertext):
            if value is not None and (type(value) is not str or len(value.encode()) > 32768):
                raise ValueError
        if any(type(value) is not str for value in (row.provider, row.base_url, row.model)):
            raise ValueError
        for value in (row.created_at, row.updated_at):
            if type(value) is not datetime or value.utcoffset() is None:
                raise ValueError
        if row.updated_at < row.created_at:
            raise ValueError
        return CapturedUserLLMConfig(row)
    except (TypeError, ValueError, UnicodeError, OverflowError):
        raise UserConfigCaptureUnavailable("user_config_capture_unavailable") from None


class LLMConfigCommitDeadlineExceeded(TimeoutError):
    pass


@dataclass(slots=True)
class FencedLLMConfigCommit:
    config: "PersistedLLMConfig"
    operation: Any


@dataclass(slots=True)
class PersistedLLMConfig:
    provider: str
    base_url: str
    model: str
    api_key: str
    updated_at: Optional[float] = None

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    def __repr__(self) -> str:
        return (
            f"PersistedLLMConfig(provider={self.provider!r}, "
            f"base_url={self.base_url!r}, model={self.model!r}, "
            f"api_key=<redacted>)"
        )


# Must match credential_manager's key resolution exactly
def _resolve_fernet(data_dir: Optional[str] = None) -> Fernet:
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
        self._cache: Dict[str, tuple] = {}

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
        self._cache.pop(user_id, None)

    def _encrypt_key(self, api_key: str) -> Optional[str]:
        if not api_key:
            return None
        return self._fernet.encrypt(api_key.encode()).decode()

    def _decrypt_key(self, api_key_enc: Optional[str]) -> str:
        if not api_key_enc:
            return ""
        return self._fernet.decrypt(api_key_enc.encode()).decode()

    def get_sync(self, user_id: str) -> Optional[PersistedLLMConfig]:
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

    def capture_user_sync(self, user_id: str) -> CapturedUserLLMConfig | None:
        try:
            _capture_user_row(None, user_id)
            with self._repository.transaction() as transaction:
                sessions = self._repository.plane_runtime.repositories.history.sessions
                sessions.bound_request_execution_waits(transaction)
                row = self._repository.repository.get_user(transaction, owner_id=user_id)
                return _capture_user_row(row, user_id)
        except Exception:
            raise UserConfigCaptureUnavailable("user_config_capture_unavailable") from None

    async def capture_user(self, user_id: str) -> CapturedUserLLMConfig | None:
        return await asyncio.to_thread(self.capture_user_sync, user_id)

    def open_captured_user_key(self, capture: CapturedUserLLMConfig) -> str:
        try:
            if type(capture) is not CapturedUserLLMConfig:
                raise ValueError
            return self._decrypt_key(capture._record.api_key_ciphertext)
        except (InvalidToken, ValueError, TypeError, UnicodeError):
            raise UserConfigCaptureUnavailable("user_config_capture_unavailable") from None

    def set_sync(self, user_id: str, *, provider: str, base_url: str,
                 model: str, api_key: str) -> PersistedLLMConfig:
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

    def _row_to_config(self, row: Optional[EncryptedLLMConfigRecord], *, discard_scope: str,
                       discard_id: str) -> Optional[PersistedLLMConfig]:
        if row is None:
            return None
        try:
            api_key = self._decrypt_key(row.api_key_ciphertext)
        except (InvalidToken, ValueError, TypeError):
            # Sync code can't await the recorder; queued for async drain
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
            except Exception:  # pragma: no cover
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
        pending = getattr(self, "_pending_discards", None)
        if pending:
            return pending.pop(0)
        return None
