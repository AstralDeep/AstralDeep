"""Snapshot harness: compiles the frozen corpus into an in-memory store with production
row shapes, signing/tamper semantics matching repository.py, and reinforcement
accounting, then binds it to the real MemoryTools read paths so every variant measures
the actual owner/project, temporal, and tamper filters.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from personalization import living_memory as lm
from personalization import memory_guard
from personalization.memory_tools import MemoryTools
from personalization.phi_gate import PHIGate

from .corpus import MemoryCase, MemoryRowSpec


class _CleanAnalyzer:
    def analyze(self, text, language, entities, score_threshold):
        return []


def clean_phi_gate() -> PHIGate:
    return PHIGate(analyzer=_CleanAnalyzer())


class CorpusStore:
    def __init__(self, cases: Tuple[MemoryCase, ...], *, hmac_key_present: bool) -> None:
        self._rows_by_owner: Dict[str, List[Dict[str, Any]]] = {}
        self._rows_by_id: Dict[str, Dict[str, Any]] = {}
        self._links: Dict[str, Dict[str, set]] = {}
        self.recall_requests: List[Tuple[str, str]] = []
        for case in cases:
            for spec in case.rows:
                row = self._materialize(spec, hmac_key_present)
                self._rows_by_id[row["id"]] = row
                self._rows_by_owner.setdefault(row["user_id"], []).append(row)
        for case in cases:
            for from_id, to_id in case.links:
                self._links.setdefault(case.owner_id, {}).setdefault(from_id, set()).add(to_id)

    @staticmethod
    def _materialize(spec: MemoryRowSpec, hmac_key_present: bool) -> Dict[str, Any]:
        signature: Optional[str] = None
        if hmac_key_present:
            if spec.tampered:
                signature = "f" * 64
            else:
                signature = memory_guard.sign_fields(
                    spec.row_id, spec.owner_id, spec.category, spec.value, spec.source)
        return {
            "id": spec.row_id,
            "user_id": spec.owner_id,
            "category": spec.category,
            "value": spec.value,
            "source": spec.source,
            "salience": spec.salience,
            "created_at": max(spec.valid_from or 0, 1_700_000_000_000),
            "updated_at": 1_700_000_000_000,
            "superseded_by": None,
            "superseded_at": None,
            "keywords": spec.keywords,
            "signature": signature,
            "valid_from": spec.valid_from,
            "valid_to": spec.valid_to,
            "ingested_at": None,
            "recall_count": 0,
            "last_recalled_at": None,
            "project_id": spec.project_id,
            "relevance": spec.relevance,
            "forbidden": spec.forbidden,
            "rationale": spec.rationale,
            "declared_tampered": spec.tampered,
        }

    def list_memory(self, user_id: str, *, project_id: Optional[str] = None,
                    include_global: bool = True) -> List[Dict[str, Any]]:
        from personalization import project_scope as ps

        rows = [dict(row) for row in self._rows_by_owner.get(user_id, [])]
        if project_id is None:
            return rows
        return ps.filter_to_project(rows, project_id, include_global=include_global)

    def get_memory(self, user_id: str, mem_id: str) -> Optional[Dict[str, Any]]:
        row = self._rows_by_id.get(mem_id)
        if row is None or row["user_id"] != user_id:
            return None
        return dict(row)

    def list_links(self, user_id: str) -> List[Dict[str, str]]:
        edges = self._links.get(user_id, {})
        return [
            {"memory_id": from_id, "linked_id": to_id}
            for from_id, targets in sorted(edges.items())
            for to_id in sorted(targets)
        ]

    def linked_ids(self, user_id: str, mem_id: str) -> List[str]:
        return sorted(self._links.get(user_id, {}).get(mem_id, set()))

    def record_recall(self, user_id: str, mem_id: str, now: Optional[int] = None) -> bool:
        self.recall_requests.append((user_id, mem_id))
        return True

    def delete_row(self, user_id: str, mem_id: str) -> bool:
        row = self._rows_by_id.get(mem_id)
        if row is None or row["user_id"] != user_id:
            return False
        del self._rows_by_id[mem_id]
        self._rows_by_owner[row["user_id"]] = [
            r for r in self._rows_by_owner[row["user_id"]] if r["id"] != mem_id]
        return True


def detect_defenses() -> Dict[str, bool]:
    guard_on = memory_guard.guard_enabled()
    return {
        "temporal_filter": lm.temporal_enabled(),
        "memory_guard": guard_on,
        "tamper_detection": guard_on and memory_guard._hmac_key() is not None,
        "project_scope": _project_scope_enabled(),
    }


def _project_scope_enabled() -> bool:
    import os

    return os.getenv("FF_PROJECT_MEMORY", "false").strip().lower() in ("1", "true", "yes", "on")


def build_tools(case: MemoryCase) -> Tuple[MemoryTools, CorpusStore]:
    store = CorpusStore((case,), hmac_key_present=memory_guard._hmac_key() is not None)
    return MemoryTools(store, phi_gate=clean_phi_gate()), store

