"""Explicit test-only draft boundary over the retiring legacy fixture database."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from typing import Any, Mapping


_DRAFT_TRANSITION_OUTCOMES = frozenset({"applied", "conflict", "failed", "replayed"})
_MAX_PLAN_JSON = 1_000_000
_MAX_CONSTITUTION_VERSION = 128


def _stable_target_agent_id(draft_id: str) -> str:
    digest = hashlib.sha256(
        b"astraldeep.draft-target/v1\0" + draft_id.encode("utf-8")
    ).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


def _canonical_uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a canonical UUID string")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a canonical UUID string") from exc
    canonical = str(parsed)
    if value != canonical:
        raise ValueError(f"{field} must be a canonical UUID string")
    return canonical


def _optional_text(value: object, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if len(value) > maximum:
        raise ValueError(f"{field} exceeds its maximum length")
    return value


class InMemoryDraftStore:
    """Thread-safe detached draft store for Deep policy tests."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._transitions: dict[str, dict[str, Any]] = {}

    def create_draft_agent(
        self,
        *,
        target_agent_id: str | None = None,
        plan_json: str | None = None,
        constitution_version: str | None = None,
        **values: object,
    ) -> None:
        draft_id = str(values["draft_id"])
        revises_agent_id = values.get("revises_agent_id")
        if target_agent_id is not None:
            target_agent_id = _canonical_uuid(target_agent_id, "target_agent_id")
            if revises_agent_id is not None and target_agent_id != revises_agent_id:
                raise ValueError("target_agent_id must match revises_agent_id")
        selected_target_agent_id = target_agent_id or (
            str(revises_agent_id)
            if revises_agent_id is not None
            else _stable_target_agent_id(draft_id)
        )
        plan_json = _optional_text(plan_json, "plan_json", _MAX_PLAN_JSON)
        constitution_version = _optional_text(
            constitution_version,
            "constitution_version",
            _MAX_CONSTITUTION_VERSION,
        )
        with self._lock:
            if draft_id in self.rows:
                raise ValueError("duplicate draft")
            now = int(time.time() * 1000)
            self.rows[draft_id] = {
                **values,
                "id": draft_id,
                "user_id": str(values["user_id"]),
                "status": "pending",
                "generation_log": None,
                "security_report": None,
                "error_message": None,
                "port": None,
                "review_notes": None,
                "reviewed_by": None,
                "refinement_history": None,
                "validation_report": None,
                "required_credentials": None,
                "self_test": None,
                "phase": None,
                "clarify_answers": None,
                "plan_json": plan_json,
                "analyze_result": None,
                "constitution_version": constitution_version,
                "host_binding": None,
                "draft_uuid": draft_id,
                "target_agent_id": selected_target_agent_id,
                "state_revision": 0,
                "generation_claim_id": None,
                "generation_claim_expires_at": None,
                "published_revision_id": None,
                "created_at": now,
                "updated_at": now,
            }

    def get_draft_agent(self, draft_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.rows.get(draft_id)
            return None if row is None else dict(row)

    def get_owned_draft_agent(
        self,
        owner_user_id: str,
        draft_id: str,
    ) -> dict[str, Any] | None:
        row = self.get_draft_agent(draft_id)
        if row is None or row.get("user_id") != owner_user_id:
            return None
        return row

    def get_user_draft_agents(self, owner_user_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = [
                dict(row)
                for row in self.rows.values()
                if row.get("user_id") == owner_user_id
                and row.get("status") not in {"live", "rejected"}
            ]
        return sorted(rows, key=lambda row: -int(row.get("created_at") or 0))

    def list_byo_sessions(
        self,
        owner_user_id: str,
        *,
        origin: str,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = [
                dict(row)
                for row in self.rows.values()
                if row.get("user_id") == owner_user_id and row.get("origin") == origin
            ]
        rows.sort(key=lambda row: -int(row.get("updated_at") or 0))
        return rows[:limit]

    def list_expired_draft_generations_for_administration(
        self,
        *,
        limit: int = 100,
        after_generation_claim_expires_at: int | None = None,
        after_draft_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return a bounded deterministic local-clock analogue for policy tests."""

        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be in 1..1000")
        supplied_cursor = (
            after_generation_claim_expires_at is not None,
            after_draft_id is not None,
        )
        if any(supplied_cursor) and not all(supplied_cursor):
            raise ValueError(
                "expired generation claim cursor fields must be supplied together"
            )
        cursor: tuple[int, str] | None = None
        if all(supplied_cursor):
            if type(after_generation_claim_expires_at) is not int:
                raise ValueError("after_generation_claim_expires_at must be an integer")
            if (
                not isinstance(after_draft_id, str)
                or not after_draft_id.strip()
                or len(after_draft_id) > 512
            ):
                raise ValueError("after_draft_id must be a bounded non-empty string")
            cursor = (after_generation_claim_expires_at, after_draft_id)
        now = int(time.time() * 1000)
        with self._lock:
            rows = [
                dict(row)
                for row in self.rows.values()
                if row.get("status") == "generating"
                and row.get("generation_claim_id") is not None
                and row.get("generation_claim_expires_at") is not None
                and int(row["generation_claim_expires_at"]) <= now
                and row.get("published_revision_id") is None
            ]
        rows.sort(
            key=lambda row: (
                int(row["generation_claim_expires_at"]),
                str(row["id"]),
            )
        )
        if cursor is not None:
            rows = [
                row
                for row in rows
                if (
                    int(row["generation_claim_expires_at"]),
                    str(row["id"]),
                )
                > cursor
            ]
        return rows[:limit]

    def update_draft_agent(self, draft_id: str, **updates: object) -> bool:
        with self._lock:
            row = self.rows.get(draft_id)
            if row is None:
                return False
            row.update(updates)
            row["updated_at"] = int(time.time() * 1000)
        return True

    def append_generation_log(
        self,
        draft_id: str,
        message: str,
        *,
        owner_user_id: str | None = None,
        expected_revision: int | None = None,
        claim_id: str | None = None,
    ) -> bool:
        supplied_fences = (
            owner_user_id is not None,
            expected_revision is not None,
            claim_id is not None,
        )
        if any(supplied_fences) and not all(supplied_fences):
            raise ValueError("generation log claim fences must be supplied together")
        fenced = all(supplied_fences)
        with self._lock:
            row = self.rows.get(draft_id)
            if row is None:
                return False
            now = int(time.time() * 1000)
            if fenced and (
                row.get("user_id") != owner_user_id
                or int(row.get("state_revision") or 0) != expected_revision
                or row.get("generation_claim_id") != claim_id
                or row.get("generation_claim_expires_at") is None
                or int(row.get("generation_claim_expires_at") or 0) <= now
                or row.get("status") != "generating"
                or row.get("published_revision_id") is not None
            ):
                return False
            current = row.get("generation_log")
            if isinstance(current, str):
                try:
                    entries = json.loads(current)
                except (TypeError, ValueError):
                    entries = []
            else:
                entries = []
            if not isinstance(entries, list):
                entries = []
            entries.append({"message": message, "timestamp": now})
            row["generation_log"] = json.dumps(entries)
            row["updated_at"] = now
        return True

    def claim_draft_generation(
        self,
        *,
        draft_id: str,
        owner_user_id: str,
        expected_revision: int,
        claim_id: str,
        lease_seconds: int = 300,
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self.rows.get(draft_id)
            now = int(time.time() * 1000)
            if (
                row is None
                or row.get("user_id") != owner_user_id
                or int(row.get("state_revision") or 0) != expected_revision
            ):
                return None
            active_claim = row.get("generation_claim_id")
            expires_at = int(row.get("generation_claim_expires_at") or 0)
            if active_claim not in (None, claim_id) and expires_at > now:
                return None
            row.update(
                {
                    "generation_claim_id": claim_id,
                    "generation_claim_expires_at": now + lease_seconds * 1000,
                    "status": "generating",
                    "error_message": None,
                    "state_revision": expected_revision + 1,
                    "updated_at": now,
                }
            )
            return dict(row)

    def finish_draft_generation(
        self,
        *,
        draft_id: str,
        owner_user_id: str,
        expected_revision: int,
        claim_id: str,
        status: str,
        error_message: str | None = None,
        security_report: str | None = None,
        validation_report: str | None = None,
        required_credentials: str | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self.rows.get(draft_id)
            now = int(time.time() * 1000)
            if (
                row is None
                or row.get("user_id") != owner_user_id
                or int(row.get("state_revision") or 0) != expected_revision
                or row.get("generation_claim_id") != claim_id
                or int(row.get("generation_claim_expires_at") or 0) <= now
            ):
                return None
            row.update(
                {
                    "generation_claim_id": None,
                    "generation_claim_expires_at": None,
                    "status": status,
                    "error_message": error_message,
                    "security_report": security_report,
                    "validation_report": validation_report,
                    "required_credentials": required_credentials,
                    "state_revision": expected_revision + 1,
                    "updated_at": now,
                }
            )
            return dict(row)

    def renew_draft_generation(
        self,
        *,
        draft_id: str,
        owner_user_id: str,
        expected_revision: int,
        claim_id: str,
        lease_seconds: int = 300,
    ) -> dict[str, Any] | None:
        """Renew one exact live test claim without changing its revision."""

        with self._lock:
            row = self.rows.get(draft_id)
            now = int(time.time() * 1000)
            if (
                row is None
                or row.get("user_id") != owner_user_id
                or int(row.get("state_revision") or 0) != expected_revision
                or row.get("generation_claim_id") != claim_id
                or int(row.get("generation_claim_expires_at") or 0) <= now
            ):
                return None
            row["generation_claim_expires_at"] = now + lease_seconds * 1000
            row["updated_at"] = now
            return dict(row)

    def reclaim_expired_draft_generation(
        self,
        *,
        draft_id: str,
        owner_user_id: str,
        expected_revision: int,
        claim_id: str,
        lease_seconds: int = 300,
    ) -> dict[str, Any] | None:
        """Reselect one expired exact test claim and advance its revision."""

        with self._lock:
            row = self.rows.get(draft_id)
            now = int(time.time() * 1000)
            if (
                row is None
                or row.get("user_id") != owner_user_id
                or int(row.get("state_revision") or 0) != expected_revision
                or row.get("generation_claim_id") != claim_id
                or row.get("generation_claim_expires_at") is None
                or int(row.get("generation_claim_expires_at") or 0) > now
                or row.get("status") != "generating"
                or row.get("published_revision_id") is not None
            ):
                return None
            row["generation_claim_expires_at"] = now + lease_seconds * 1000
            row["state_revision"] = expected_revision + 1
            row["updated_at"] = now
            return dict(row)

    def get_exact_live_draft_generation_claim(
        self,
        *,
        draft_id: str,
        owner_user_id: str,
        expected_preclaim_revision: int,
        claim_id: str,
    ) -> dict[str, Any] | None:
        """Return the exact live post-claim row after acknowledgement loss."""

        with self._lock:
            row = self.rows.get(draft_id)
            now = int(time.time() * 1000)
            if (
                row is None
                or row.get("user_id") != owner_user_id
                or int(row.get("state_revision") or 0) != expected_preclaim_revision + 1
                or row.get("generation_claim_id") != claim_id
                or int(row.get("generation_claim_expires_at") or 0) <= now
                or row.get("status") != "generating"
                or row.get("published_revision_id") is not None
            ):
                return None
            return dict(row)

    def delete_draft_agent(self, draft_id: str) -> bool:
        with self._lock:
            return self.rows.pop(draft_id, None) is not None

    def get_exact_draft_transition(
        self,
        *,
        draft_id: str,
        owner_user_id: str,
        transition_id: str,
        transition_kind: str,
        expected_revision: int,
    ) -> tuple[int, str] | None:
        """Return one exact stored transition without comparing observer state."""

        with self._lock:
            current = self.rows.get(draft_id)
            replay = self._transitions.get(transition_id)
            if current is None or replay is None:
                return None
            result_revision = replay.get("result_revision")
            outcome = replay.get("outcome")
            if (
                current.get("user_id") != owner_user_id
                or replay.get("draft_uuid") != current.get("draft_uuid")
                or replay.get("owner_user_id") != owner_user_id
                or replay.get("transition_kind") != transition_kind
                or replay.get("expected_revision") != expected_revision
                or type(result_revision) is not int
                or result_revision < 0
                or result_revision > int(current.get("state_revision") or 0)
                or outcome not in _DRAFT_TRANSITION_OUTCOMES
            ):
                return None
            if outcome in {"applied", "replayed"} and (
                result_revision != expected_revision + 1
            ):
                return None
            if outcome == "conflict" and result_revision == expected_revision:
                return None
            return result_revision, outcome

    def compare_and_set_with_transition(
        self,
        *,
        draft_id: str,
        owner_user_id: str,
        expected_revision: int,
        updates: Mapping[str, object],
        transition_kind: str,
        transition_id: str | None,
        operation_fence: object | None,
    ) -> tuple[str, int, dict[str, Any]]:
        del operation_fence
        with self._lock:
            current = self.get_owned_draft_agent(owner_user_id, draft_id)
            if current is None:
                raise LookupError("draft is unavailable")
            if transition_id is not None and transition_id in self._transitions:
                replay = self._transitions[transition_id]
                same = (
                    replay["draft_id"] == draft_id
                    and replay["owner_user_id"] == owner_user_id
                    and replay["transition_kind"] == transition_kind
                    and replay["expected_revision"] == expected_revision
                )
                if not same:
                    return "conflict", int(current["state_revision"]), current
                return "replayed", int(replay["result_revision"]), current
            current_revision = int(current.get("state_revision") or 0)
            if current_revision != expected_revision:
                return "conflict", current_revision, current
            result_revision = current_revision + 1
            self.rows[draft_id].update(updates)
            self.rows[draft_id]["state_revision"] = result_revision
            self.rows[draft_id]["updated_at"] = int(time.time() * 1000)
            if transition_id is not None:
                self._transitions[transition_id] = {
                    "draft_id": draft_id,
                    "draft_uuid": current.get("draft_uuid"),
                    "owner_user_id": owner_user_id,
                    "transition_kind": transition_kind,
                    "expected_revision": expected_revision,
                    "result_revision": result_revision,
                    "outcome": "applied",
                }
            return "applied", result_revision, dict(self.rows[draft_id])


class DatabaseDraftStoreDouble:
    """Expose the typed Deep draft-store shape without a production fallback."""

    def __init__(self, database: Any) -> None:
        self.database = database
        self._lock = threading.RLock()
        self._transitions: dict[str, dict[str, Any]] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self.database, name)

    def create_draft_agent(
        self,
        *,
        plan_json: str | None = None,
        constitution_version: str | None = None,
        **values: object,
    ) -> Any:
        attachment_id = values.pop("source_attachment_id", None)
        plan_json = _optional_text(plan_json, "plan_json", _MAX_PLAN_JSON)
        constitution_version = _optional_text(
            constitution_version,
            "constitution_version",
            _MAX_CONSTITUTION_VERSION,
        )
        created = self.database.create_draft_agent(**values)
        initial_updates = {
            name: value
            for name, value in (
                ("source_attachment_id", attachment_id),
                ("plan_json", plan_json),
                ("constitution_version", constitution_version),
            )
            if value is not None
        }
        if initial_updates:
            self.database.update_draft_agent(
                str(values["draft_id"]),
                **initial_updates,
            )
        return created

    def get_owned_draft_agent(
        self,
        owner_user_id: str,
        draft_id: str,
    ) -> dict[str, Any] | None:
        row = self.database.get_draft_agent(draft_id)
        if row is None or row.get("user_id") != owner_user_id:
            return None
        return dict(row)

    def list_byo_sessions(
        self,
        owner_user_id: str,
        *,
        origin: str,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            "SELECT * FROM draft_agents WHERE user_id = ? AND origin = ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (owner_user_id, origin, limit),
        )
        return [dict(row) for row in rows]

    def compare_and_set_with_transition(
        self,
        *,
        draft_id: str,
        owner_user_id: str,
        expected_revision: int,
        updates: Mapping[str, object],
        transition_kind: str,
        transition_id: str | None,
        operation_fence: object | None,
    ) -> tuple[str, int, dict[str, Any]]:
        del operation_fence
        with self._lock:
            current = self.get_owned_draft_agent(owner_user_id, draft_id)
            if current is None:
                raise LookupError("draft is unavailable")
            if transition_id is not None and transition_id in self._transitions:
                replay = self._transitions[transition_id]
                same = (
                    replay["draft_id"] == draft_id
                    and replay["owner_user_id"] == owner_user_id
                    and replay["transition_kind"] == transition_kind
                    and replay["expected_revision"] == expected_revision
                )
                if not same:
                    return "conflict", int(current["state_revision"]), current
                return "replayed", int(replay["result_revision"]), current
            current_revision = int(current.get("state_revision") or 0)
            if current_revision != expected_revision:
                return "conflict", current_revision, current
            result_revision = current_revision + 1
            self.database.update_draft_agent(
                draft_id,
                **dict(updates),
                state_revision=result_revision,
            )
            updated = dict(self.database.get_draft_agent(draft_id))
            if transition_id is not None:
                self._transitions[transition_id] = {
                    "draft_id": draft_id,
                    "draft_uuid": current.get("draft_uuid"),
                    "owner_user_id": owner_user_id,
                    "transition_kind": transition_kind,
                    "expected_revision": expected_revision,
                    "result_revision": result_revision,
                    "outcome": "applied",
                }
            return "applied", result_revision, updated

    def get_exact_draft_transition(
        self,
        *,
        draft_id: str,
        owner_user_id: str,
        transition_id: str,
        transition_kind: str,
        expected_revision: int,
    ) -> tuple[int, str] | None:
        """Return one exact stored transition without comparing observer state."""

        with self._lock:
            current = self.get_owned_draft_agent(owner_user_id, draft_id)
            replay = self._transitions.get(transition_id)
            if current is None or replay is None:
                return None
            result_revision = replay.get("result_revision")
            outcome = replay.get("outcome")
            if (
                replay.get("draft_uuid") != current.get("draft_uuid")
                or replay.get("owner_user_id") != owner_user_id
                or replay.get("transition_kind") != transition_kind
                or replay.get("expected_revision") != expected_revision
                or type(result_revision) is not int
                or result_revision < 0
                or result_revision > int(current.get("state_revision") or 0)
                or outcome not in _DRAFT_TRANSITION_OUTCOMES
            ):
                return None
            if outcome in {"applied", "replayed"} and (
                result_revision != expected_revision + 1
            ):
                return None
            if outcome == "conflict" and result_revision == expected_revision:
                return None
            return result_revision, outcome
