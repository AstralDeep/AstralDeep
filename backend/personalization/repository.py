"""Persistence facade for personalization, personality, and durable memory.

All durable operations delegate to typed AstralPlane repositories and remain
strictly user-scoped.
PHI gating is applied by callers (service / memory_tools) before values reach
this layer — the repository is dumb persistence.

"""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

from astralplane.repositories.personalization_graph import (
    ConsolidationSweepRecord,
    ShortTermSignalRecord,
)
from astralplane.repositories.preferences import (
    MemoryRecord,
    PersonalizationProfileRecord,
)
from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)

MEMORY_CATEGORIES = ("profession", "goal", "preference", "workflow_tag", "context")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _after_ms(previous: int) -> int:
    return max(_now_ms(), previous + 1)


class PersonalizationRepository:
    def __init__(
        self,
        db,
        *,
        plane_runtime=None,
        plane_repositories=None,
        graph_repository=None,
        preferences_repository=None,
    ) -> None:
        self.db = db
        self._graph: PlaneRepositoryContext | None = None
        self._personalization: PlaneRepositoryContext | None = None
        if db is not None or plane_runtime is not None:
            repository, runtime = repository_from(
                "personalization_graph",
                plane_runtime=plane_runtime,
                repositories=plane_repositories,
                legacy_database=db,
            )
            self._graph = PlaneRepositoryContext(
                repository=graph_repository or repository,
                plane_runtime=runtime,
                legacy_database=db,
            )
            preferences, preferences_runtime = repository_from(
                "preferences",
                plane_runtime=plane_runtime,
                repositories=plane_repositories,
                legacy_database=db,
            )
            self._personalization = PlaneRepositoryContext(
                repository=(preferences_repository or preferences).personalization,
                plane_runtime=preferences_runtime,
                legacy_database=db,
            )

    def _graph_context(self) -> PlaneRepositoryContext:
        if self._graph is None:
            raise RuntimeError("personalization graph repository is not bound")
        return self._graph

    def _personalization_context(self) -> PlaneRepositoryContext:
        if self._personalization is None:
            raise RuntimeError("personalization repository is not bound")
        return self._personalization

    # ── Profile / personality ────────────────────────────────────────────

    def get_profile(self, user_id: str) -> Optional[Dict[str, Any]]:
        context = self._personalization_context()
        record = context.call(
            context.repository.get_profile,
            owner_id=user_id,
        )
        return None if record is None else _profile_to_dict(record)

    def upsert_profile(
        self,
        user_id: str,
        *,
        profession: Optional[str] = None,
        goals: Optional[List[str]] = None,
        personality: Optional[Dict[str, Any]] = None,
        dreaming_enabled: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Insert or update the user's profile. Only provided fields change."""
        context = self._personalization_context()
        with context.transaction() as transaction:
            existing = context.repository.get_profile(
                transaction,
                owner_id=user_id,
            )
            if existing is None:
                now = _now_ms()
                candidate = PersonalizationProfileRecord(
                    owner_id=user_id,
                    profession=profession,
                    goals=tuple(goals or ()),
                    personality=dict(personality or {}),
                    dreaming_enabled=(
                        True if dreaming_enabled is None else bool(dreaming_enabled)
                    ),
                    created_at=now,
                    updated_at=now,
                )
                expected_updated_at = None
            else:
                candidate = PersonalizationProfileRecord(
                    owner_id=user_id,
                    profession=(
                        existing.profession if profession is None else profession
                    ),
                    goals=(existing.goals if goals is None else tuple(goals)),
                    personality=(
                        existing.personality
                        if personality is None
                        else dict(personality)
                    ),
                    dreaming_enabled=(
                        existing.dreaming_enabled
                        if dreaming_enabled is None
                        else bool(dreaming_enabled)
                    ),
                    created_at=existing.created_at,
                    updated_at=_after_ms(existing.updated_at),
                )
                expected_updated_at = existing.updated_at
            record = context.repository.put_profile(
                transaction,
                candidate,
                expected_updated_at=expected_updated_at,
            )
        return _profile_to_dict(record)

    def reset_profile(self, user_id: str) -> None:
        """Reset a user's profile/personality to defaults (keeps the row)."""
        context = self._personalization_context()
        with context.transaction() as transaction:
            existing = context.repository.get_profile(
                transaction,
                owner_id=user_id,
            )
            if existing is None:
                return
            context.repository.reset_profile(
                transaction,
                owner_id=user_id,
                expected_updated_at=existing.updated_at,
                updated_at=_after_ms(existing.updated_at),
            )

    def set_dreaming_enabled(self, user_id: str, enabled: bool) -> None:
        # Ensure a row exists, then set the flag.
        self.upsert_profile(user_id, dreaming_enabled=enabled)

    # ── Durable memory ───────────────────────────────────────────────────

    def list_memory(self, user_id: str, *, project_id: Optional[str] = None,
                    include_global: bool = True) -> List[Dict[str, Any]]:
        # Superseded (soft-deleted / replaced) memories are excluded from all
        # recall — reconciliation keeps the live set clean.
        #
        # C-U9 — ``project_id`` semantics:
        #   * None          → NO filter, every live row (legacy / flag-off path).
        #   * GLOBAL sentinel → the global slice only (project_id IS NULL).
        #   * a concrete id → that project's rows plus (when ``include_global``)
        #                     the untagged/global ones; private rows never leak.
        from .project_scope import GLOBAL

        context = self._personalization_context()
        records = context.call(
            context.repository.list_memory,
            owner_id=user_id,
            project_id=None if project_id in (None, GLOBAL) else project_id,
            include_global=include_global,
            global_only=project_id == GLOBAL,
            limit=1000,
        )
        return [_memory_to_dict(record) for record in records]

    # ── Living memory seams (temporal / recall / persona) ──

    def set_validity(self, user_id: str, mem_id: str, *, valid_from=None,
                     valid_to=None, ingested_at=None) -> bool:
        """C-M6: set a memory's temporal-validity bounds (epoch-ms; NULL = open)."""
        context = self._personalization_context()
        with context.transaction() as transaction:
            existing = context.repository.get_memory(
                transaction,
                owner_id=user_id,
                memory_id=mem_id,
            )
            if existing is None:
                return False
            record = context.repository.set_validity(
                transaction,
                owner_id=user_id,
                memory_id=mem_id,
                valid_from=valid_from,
                valid_to=valid_to,
                ingested_at=ingested_at,
                expected_updated_at=existing.updated_at,
                updated_at=_after_ms(existing.updated_at),
            )
        return record is not None

    def record_recall(self, user_id: str, mem_id: str, now: Optional[int] = None) -> bool:
        """C-M7: reinforcement-on-recall — bump recall_count and reset the decay
        clock (last_recalled_at). Idempotent per call."""
        ts = now if now is not None else _now_ms()
        context = self._personalization_context()
        record = context.call(
            context.repository.record_recall,
            owner_id=user_id,
            memory_id=mem_id,
            recalled_at=ts,
        )
        return record is not None

    def get_persona(self, user_id: str) -> Optional[Dict[str, Any]]:
        """C-M8: the user's current evolving persona row (or None)."""
        context = self._personalization_context()
        record = context.call(
            context.repository.get_persona,
            owner_id=user_id,
        )
        return None if record is None else {
            "user_id": record.owner_id,
            "persona": record.persona,
            "score": record.score,
            "updated_at": record.updated_at,
        }

    def set_persona(self, user_id: str, persona: str, score: float) -> None:
        """C-M8: upsert the user's persona (keep-best is decided by the caller)."""
        context = self._personalization_context()
        context.call(
            context.repository.put_persona,
            owner_id=user_id,
            persona=persona,
            score=float(score),
            updated_at=_now_ms(),
        )

    def create_memory(
        self, user_id: str, category: str, value: str, *, source: str = "explicit",
        salience: float = 0.0, keywords: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if category not in MEMORY_CATEGORIES:
            raise ValueError(f"invalid memory category: {category}")
        if source not in ("explicit", "promoted"):
            raise ValueError(f"invalid memory source: {source}")
        mem_id = str(uuid.uuid4())
        now = _now_ms()
        # HMAC-sign the row's identifying fields (None when no key set).
        # project_id is partition metadata (like keywords) — NOT part of the
        # signed identity, so pre-C-U9 signed rows stay valid.
        from .memory_guard import sign_fields
        signature = sign_fields(mem_id, user_id, category, value, source)
        context = self._personalization_context()
        record = context.call(
            context.repository.create_memory,
            record=MemoryRecord(
                memory_id=mem_id,
                owner_id=user_id,
                category=category,
                value=value,
                source=source,
                salience=salience,
                created_at=now,
                updated_at=now,
                superseded_by=None,
                superseded_at=None,
                keywords=keywords,
                signature=signature,
                valid_from=None,
                valid_to=None,
                ingested_at=None,
                recall_count=0,
                last_recalled_at=None,
                project_id=project_id,
            ),
        )
        return _memory_to_dict(record)

    def get_memory(self, user_id: str, mem_id: str) -> Optional[Dict[str, Any]]:
        context = self._personalization_context()
        record = context.call(
            context.repository.get_memory,
            owner_id=user_id,
            memory_id=mem_id,
        )
        return None if record is None else _memory_to_dict(record)

    def update_memory_value(self, user_id: str, mem_id: str, value: str) -> bool:
        from .memory_guard import sign_fields

        context = self._personalization_context()
        with context.transaction() as transaction:
            existing = context.repository.get_memory(
                transaction,
                owner_id=user_id,
                memory_id=mem_id,
            )
            if existing is None:
                return False
            record = context.repository.update_memory_value(
                transaction,
                owner_id=user_id,
                memory_id=mem_id,
                value=value,
                signature=sign_fields(
                    mem_id,
                    user_id,
                    existing.category,
                    value,
                    existing.source,
                ),
                expected_updated_at=existing.updated_at,
                updated_at=_after_ms(existing.updated_at),
            )
        return record is not None

    def delete_memory(self, user_id: str, mem_id: str) -> bool:
        context = self._personalization_context()
        with context.transaction() as transaction:
            existing = context.repository.get_memory(
                transaction,
                owner_id=user_id,
                memory_id=mem_id,
            )
            if existing is None:
                return False
            return context.repository.delete_memory(
                transaction,
                owner_id=user_id,
                memory_id=mem_id,
                expected_updated_at=existing.updated_at,
            )

    def supersede_memory(self, user_id: str, old_id: str,
                         new_id: Optional[str] = None) -> bool:
        """Soft-delete a memory (reconcile UPDATE/DELETE). Sets ``superseded_at``
        so the row drops out of recall; ``new_id`` optionally points at the
        replacement memory (UPDATE) — left NULL for a plain removal (DELETE).
        Only affects a currently-live row (idempotent)."""
        context = self._personalization_context()
        return context.call(
            context.repository.supersede_memory,
            owner_id=user_id,
            memory_id=old_id,
            replacement_id=new_id,
            superseded_at=_now_ms(),
        )

    # ── Linked-note graph ──

    def add_link(self, user_id: str, a_id: str, b_id: str) -> bool:
        """Create an undirected link between two memories (stored as both
        directed edges so a single-column lookup finds neighbours either way).
        Idempotent; a self-link is ignored."""
        if not a_id or not b_id or a_id == b_id:
            return False
        graph = self._graph_context()
        try:
            pair = graph.call(
                graph.repository.add_link,
                owner_id=user_id,
                memory_id=a_id,
                linked_id=b_id,
                created_at=_now_ms(),
            )
        except Exception:
            return False
        return len(pair) == 2

    def linked_ids(self, user_id: str, mem_id: str) -> List[str]:
        """Ids of memories linked to ``mem_id`` (live links only — superseded
        targets are filtered out by the join)."""
        graph = self._graph_context()
        return list(
            graph.call(
                graph.repository.linked_ids,
                owner_id=user_id,
                memory_id=mem_id,
                limit=1000,
            )
        )

    def list_links(self, user_id: str) -> List[Dict[str, str]]:
        """All live directed link edges for a user (both directions of each
        undirected link), filtered to live endpoints. Powers the
        Personalized-PageRank graph in one query."""
        graph = self._graph_context()
        records = graph.call(
            graph.repository.list_links,
            owner_id=user_id,
            limit=5000,
        )
        return [
            {"memory_id": record.memory_id, "linked_id": record.linked_id}
            for record in records
        ]

    # ── Short-term signals ───────────────────────────────────────────────

    def add_signal(self, user_id: str, category: str, value: str) -> Dict[str, Any]:
        if category not in MEMORY_CATEGORIES:
            raise ValueError(f"invalid signal category: {category}")
        sig_id = str(uuid.uuid4())
        now = _now_ms()
        graph = self._graph_context()
        record = graph.call(
            graph.repository.create_signal,
            record=ShortTermSignalRecord(
                signal_id=sig_id,
                owner_id=user_id,
                category=category,
                value=value,
                recall_count=1,
                last_seen_at=now,
                created_at=now,
            ),
        )
        return {
            "id": record.signal_id,
            "user_id": record.owner_id,
            "category": record.category,
            "value": record.value,
        }

    def list_signals(self, user_id: str) -> List[Dict[str, Any]]:
        graph = self._graph_context()
        records = graph.call(
            graph.repository.list_signals,
            owner_id=user_id,
            limit=1000,
        )
        return [
            {
                "id": record.signal_id,
                "user_id": record.owner_id,
                "category": record.category,
                "value": record.value,
                "recall_count": record.recall_count,
                "last_seen_at": record.last_seen_at,
                "created_at": record.created_at,
            }
            for record in records
        ]

    def delete_signal(self, user_id: str, sig_id: str) -> None:
        graph = self._graph_context()
        graph.call(
            graph.repository.delete_signal,
            owner_id=user_id,
            signal_id=sig_id,
        )

    # ── Consolidation sweeps ("dreams") ──────────────────────────────────

    def record_sweep(self, sweep: Dict[str, Any]) -> None:
        graph = self._graph_context()
        graph.call(
            graph.repository.record_sweep,
            record=ConsolidationSweepRecord(
                sweep_id=sweep["id"],
                owner_id=sweep["user_id"],
                ran_at=sweep["ran_at"],
                candidates_considered=sweep["candidates_considered"],
                promoted_count=sweep["promoted_count"],
                summary=sweep["summary"],
                trigger=sweep["trigger"],
            ),
        )

    def list_sweeps(self, user_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        graph = self._graph_context()
        records = graph.call(
            graph.repository.list_sweeps,
            owner_id=user_id,
            limit=limit,
        )
        return [
            {
                "id": record.sweep_id,
                "ran_at": record.ran_at,
                "candidates_considered": record.candidates_considered,
                "promoted_count": record.promoted_count,
                "summary": record.summary,
                "trigger": record.trigger,
            }
            for record in records
        ]


def _profile_to_dict(record: PersonalizationProfileRecord) -> Dict[str, Any]:
    return {
        "user_id": record.owner_id,
        "profession": record.profession,
        "goals": list(record.goals),
        "personality": dict(record.personality),
        "dreaming_enabled": record.dreaming_enabled,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _memory_to_dict(record: MemoryRecord) -> Dict[str, Any]:
    return {
        "id": record.memory_id,
        "user_id": record.owner_id,
        "category": record.category,
        "value": record.value,
        "source": record.source,
        "salience": record.salience,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "superseded_by": record.superseded_by,
        "superseded_at": record.superseded_at,
        "keywords": record.keywords,
        "signature": record.signature,
        "valid_from": record.valid_from,
        "valid_to": record.valid_to,
        "ingested_at": record.ingested_at,
        "recall_count": record.recall_count,
        "last_recalled_at": record.last_recalled_at,
        "project_id": record.project_id,
    }
