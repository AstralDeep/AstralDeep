"""Ephemeral, process-local embedding cache bound to owner/project, provider profile,
snapshot source digest, text digest and a monotonic TTL; expired or evicted entries are
never served, and nothing is persisted anywhere.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from typing import Callable, Dict, List, Optional, Tuple


class EphemeralEmbeddingCache:
    def __init__(self, *, max_entries: int = 4096, ttl_seconds: float = 900.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: "OrderedDict[str, Tuple[float, List[float]]]" = OrderedDict()
        self._ownership: Dict[str, Tuple[str, str]] = {}
        self.hits = 0
        self.misses = 0
        self.expirations = 0
        self.evictions = 0

    @staticmethod
    def _key(profile_digest: str, owner_id: str, project_id: Optional[str],
             source_digest: str, text: str) -> str:
        material = "\x1f".join([
            profile_digest,
            owner_id,
            project_id or "",
            source_digest,
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
        ])
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def get(self, profile_digest: str, owner_id: str, project_id: Optional[str],
            source_digest: str, text: str) -> Optional[List[float]]:
        key = self._key(profile_digest, owner_id, project_id, source_digest, text)
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        expires_at, vector = entry
        if self._clock() >= expires_at:
            del self._entries[key]
            self._ownership.pop(key, None)
            self.expirations += 1
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return list(vector)

    def put(self, profile_digest: str, owner_id: str, project_id: Optional[str],
            source_digest: str, text: str, vector: List[float]) -> None:
        key = self._key(profile_digest, owner_id, project_id, source_digest, text)
        self._entries[key] = (self._clock() + self._ttl_seconds, list(vector))
        self._ownership[key] = (owner_id, project_id or "")
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            oldest, _ = self._entries.popitem(last=False)
            self._ownership.pop(oldest, None)
            self.evictions += 1

    def invalidate_owner(self, owner_id: str, project_id: Optional[str] = None) -> int:
        dropped = 0
        for key in list(self._entries):
            entry_owner, entry_project = self._ownership.get(key, ("", ""))
            if entry_owner == owner_id and (
                    project_id is None or entry_project == (project_id or "")):
                del self._entries[key]
                self._ownership.pop(key, None)
                dropped += 1
        return dropped

    def __len__(self) -> int:
        return len(self._entries)
