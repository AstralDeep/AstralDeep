"""Stores owner-scoped decisions-provider credentials and their last-use outcome.
Legacy TypeSafe keys and versioned custom configurations use user_store.py's Fernet key.
Backs typesafe_handlers.py's save, clear, and probe flow.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import logging
import time
from typing import Any, Literal, Optional

from astralplane.repositories import (
    RepositoryError,
    RepositoryNotFoundError,
)
from cryptography.fernet import InvalidToken
from orchestrator.plane_repository_context import PlaneRepositoryContext, repository_from
from orchestrator.typesafe_routing.client import TYPESAFE_API_BASE, TYPESAFE_MODEL

from .user_store import _resolve_fernet

logger = logging.getLogger("LLMConfig.TypeSafeStore")

_CACHE_TTL_SECONDS = 30.0

MAX_KEY_CHARS = 512
_CONFIG_PREFIX = "decisions:v1:"

Outcome = Literal["valid", "rejected", "unavailable"]
StatusName = Literal["not_set", "active", "rejected", "unavailable"]


def key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class TypeSafeKeyStatus:
    name: StatusName = "not_set"
    at: Optional[datetime] = None
    last_verified_at: Optional[datetime] = None
    provider: str = "typesafe"
    base_url: str = TYPESAFE_API_BASE
    model: str = TYPESAFE_MODEL
    has_key: bool = False

    @property
    def is_set(self) -> bool:
        return self.name != "not_set"

    @property
    def is_usable(self) -> bool:
        return self.name in ("active", "unavailable")


@dataclass(frozen=True, slots=True)
class StoredTypeSafeKey:
    api_key: str = field(repr=False)
    fingerprint: str
    provider: str = "typesafe"
    base_url: str = TYPESAFE_API_BASE
    model: str = TYPESAFE_MODEL

    def __repr__(self) -> str:  # pragma: no cover
        return f"StoredTypeSafeKey(fingerprint={self.fingerprint!r})"


class TypeSafeCredentialStore:
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
        self._cache.pop(user_id, None)

    def _encrypt(self, api_key: str) -> str:
        return self._fernet.encrypt(api_key.encode("utf-8")).decode("ascii")

    def _decrypt(self, ciphertext: str) -> str:
        return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")

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
            # Read failure treated as no key, never an error
            logger.warning("TypeSafe credential read failed; treating as unset", exc_info=True)
            return None
        self._cache_put(user_id, row)
        return row

    def get_key_sync(self, user_id: str) -> Optional[StoredTypeSafeKey]:
        row = self._row_sync(user_id)
        if row is None:
            return None
        try:
            if row.api_key_ciphertext.startswith(_CONFIG_PREFIX):
                config = json.loads(self._decrypt(row.api_key_ciphertext[len(_CONFIG_PREFIX):]))
                if (not isinstance(config, dict) or set(config) != {"provider", "base_url", "model", "api_key"}
                        or config["provider"] != "custom"):
                    raise ValueError("invalid decision configuration")
                self._validate_config(**config)
                return StoredTypeSafeKey(fingerprint=row.key_fingerprint, **config)
            api_key = self._decrypt(row.api_key_ciphertext)
            if api_key.startswith("{") or len(api_key) > MAX_KEY_CHARS:
                raise ValueError("invalid legacy credential")
        except (InvalidToken, ValueError, TypeError, UnicodeError):
            self._discard_undecryptable(user_id)
            return None
        if not api_key:
            return None
        return StoredTypeSafeKey(api_key=api_key, fingerprint=row.key_fingerprint)

    async def get_key(self, user_id: str) -> Optional[StoredTypeSafeKey]:
        return await asyncio.to_thread(self.get_key_sync, user_id)

    def status_sync(self, user_id: str) -> TypeSafeKeyStatus:
        row = self._row_sync(user_id)
        if row is None:
            return TypeSafeKeyStatus()
        stored = self.get_key_sync(user_id)
        if stored is None:
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
            provider=stored.provider, base_url=stored.base_url, model=stored.model,
            has_key=bool(stored.api_key),
        )

    async def status(self, user_id: str) -> TypeSafeKeyStatus:
        return await asyncio.to_thread(self.status_sync, user_id)

    def save_sync(self, user_id: str, api_key: str) -> TypeSafeKeyStatus:
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
            name="active", at=verified_at, last_verified_at=verified_at, has_key=True
        )

    async def save(self, user_id: str, api_key: str) -> TypeSafeKeyStatus:
        return await asyncio.to_thread(self.save_sync, user_id, api_key)

    @staticmethod
    def _validate_config(*, provider: str, base_url: str, model: str, api_key: str) -> None:
        from orchestrator.typesafe_routing.custom import CustomDecisionClient

        if provider != "custom" or not isinstance(api_key, str) or len(api_key) > MAX_KEY_CHARS:
            raise ValueError("invalid decisions provider or key")
        if any(character.isspace() for character in api_key):
            raise ValueError("invalid decisions key")
        CustomDecisionClient(base_url, model)

    def save_config_sync(self, user_id: str, *, provider: str, base_url: str,
                         model: str, api_key: str) -> tuple[TypeSafeKeyStatus, str]:
        self._validate_config(provider=provider, base_url=base_url, model=model, api_key=api_key)
        config = {"provider": provider, "base_url": base_url.rstrip("/"),
                  "model": model, "api_key": api_key}
        encoded = json.dumps(config, sort_keys=True, separators=(",", ":"))
        fingerprint = key_fingerprint(encoded)
        verified_at = datetime.now(UTC)
        self._repository.call(
            self._repository.repository.upsert_user, owner_id=user_id,
            api_key_ciphertext=_CONFIG_PREFIX + self._encrypt(encoded),
            key_fingerprint=fingerprint, verified_at=verified_at,
        )
        self.invalidate(user_id)
        return TypeSafeKeyStatus(
            name="active", at=verified_at, last_verified_at=verified_at,
            provider=provider, base_url=config["base_url"], model=model, has_key=bool(api_key),
        ), fingerprint

    async def save_config(self, user_id: str, **config: Any) -> tuple[TypeSafeKeyStatus, str]:
        return await asyncio.to_thread(self.save_config_sync, user_id, **config)

    def clear_sync(self, user_id: str) -> bool:
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
        except Exception:  # pragma: no cover
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
        return await asyncio.to_thread(
            self.record_outcome_sync, user_id, outcome, fingerprint
        )

    def _discard_undecryptable(self, user_id: str) -> None:
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
        except Exception:  # pragma: no cover
            logger.exception("Failed to delete undecryptable TypeSafe credential row")
        self.invalidate(user_id)
        self._pending_discards.append(user_id)

    def pop_discard_note(self) -> Optional[str]:
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
