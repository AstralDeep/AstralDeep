"""AstralPlane-backed compatibility seam for draft-agent orchestration.

The lifecycle and guided-authoring state machines are Deep product policy.  All
durable draft, identity, ownership, and permission state belongs to Plane.  This
adapter preserves the small dictionary-shaped surface those state machines use
while ensuring every mutation is owner-scoped and revision-fenced inside one
caller-owned Plane transaction.
"""

from __future__ import annotations

import json
import hashlib
import time
import uuid
from contextlib import contextmanager
from dataclasses import fields
from datetime import datetime
from typing import Any, Iterator, Mapping

from astralplane.repositories import RepositoryConflictError
from astralplane.repositories.drafts import DraftAgentRecord


_DRAFT_TRANSITION_OUTCOMES = frozenset({"applied", "conflict", "failed", "replayed"})


def _now_ms() -> int:
    return int(time.time() * 1000)


def _stable_target_agent_id(draft_id: str) -> str:
    """Derive a replay-stable UUID4-shaped target from the immutable draft id."""

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


def draft_record_to_dict(record: DraftAgentRecord) -> dict[str, Any]:
    """Return the legacy detached mapping without exposing mutable row state."""

    if not isinstance(record, DraftAgentRecord):
        raise TypeError("record must be a DraftAgentRecord")
    result = {field.name: getattr(record, field.name) for field in fields(record)}
    result["id"] = record.draft_id
    result["user_id"] = record.owner_id
    return result


class PlaneDraftStore:
    """Synchronous typed persistence used only from Deep worker-thread seams."""

    def __init__(
        self,
        *,
        plane_runtime: Any,
        plane_repositories: Any | None = None,
        work_admission: Any | None = None,
    ) -> None:
        if not callable(getattr(plane_runtime, "transaction", None)):
            raise TypeError("plane_runtime must expose transaction()")
        repositories = plane_repositories or getattr(
            plane_runtime,
            "repositories",
            None,
        )
        if repositories is None:
            raise TypeError("Plane repository catalog is required")
        required = ("draft_agents", "identity", "agents", "tool_policy_state")
        missing = tuple(name for name in required if not hasattr(repositories, name))
        if missing:
            raise TypeError(
                "Plane repository catalog is incomplete: " + ", ".join(missing)
            )
        self._runtime = plane_runtime
        self._drafts = repositories.draft_agents
        self._identity = repositories.identity
        self._agents = repositories.agents
        self._tool_policy = repositories.tool_policy_state
        self._work_admission = work_admission

    @contextmanager
    def _transaction(self, operation_fence: Any | None = None) -> Iterator[Any]:
        if operation_fence is None:
            with self._runtime.transaction() as transaction:
                yield transaction
            return
        if self._work_admission is None:
            raise RuntimeError("draft operation fence authority is unavailable")
        with self._work_admission.fenced_transaction(operation_fence) as transaction:
            yield transaction

    def create_draft_agent(
        self,
        draft_id: str,
        user_id: str,
        agent_name: str,
        agent_slug: str,
        description: str,
        tools_spec: str | None = None,
        skill_tags: str | None = None,
        packages: str | None = None,
        origin: str = "manual",
        source_chat_id: str | None = None,
        gap_fingerprint: str | None = None,
        source_attachment_id: str | None = None,
        revises_agent_id: str | None = None,
        target_agent_id: str | None = None,
        plan_json: str | None = None,
        constitution_version: str | None = None,
    ) -> dict[str, Any]:
        if target_agent_id is not None:
            target_agent_id = _canonical_uuid(target_agent_id, "target_agent_id")
            if revises_agent_id is not None and target_agent_id != revises_agent_id:
                raise ValueError("target_agent_id must match revises_agent_id")
        selected_target_agent_id = (
            target_agent_id or revises_agent_id or _stable_target_agent_id(draft_id)
        )
        with self._runtime.transaction() as transaction:
            record = self._drafts.create_draft(
                transaction,
                draft_id=draft_id,
                owner_id=user_id,
                agent_name=agent_name,
                agent_slug=agent_slug,
                description=description,
                tools_spec=tools_spec,
                skill_tags=skill_tags,
                packages=packages,
                origin=origin,
                source_chat_id=source_chat_id,
                gap_fingerprint=gap_fingerprint,
                source_attachment_id=source_attachment_id,
                revises_agent_id=revises_agent_id,
                plan_json=plan_json,
                constitution_version=constitution_version,
                draft_uuid=str(uuid.UUID(draft_id)),
                target_agent_id=selected_target_agent_id,
                observed_at=_now_ms(),
            )
        return draft_record_to_dict(record)

    def get_owned_draft_agent(
        self,
        owner_user_id: str,
        draft_id: str,
    ) -> dict[str, Any] | None:
        with self._runtime.transaction() as transaction:
            record = self._drafts.get_draft(
                transaction,
                owner_id=owner_user_id,
                draft_id=draft_id,
            )
        return None if record is None else draft_record_to_dict(record)

    def get_draft_agent(self, draft_id: str) -> dict[str, Any] | None:
        with self._runtime.transaction() as transaction:
            record = self._drafts.get_draft_for_administration(
                transaction,
                draft_id=draft_id,
            )
        return None if record is None else draft_record_to_dict(record)

    def get_draft_agent_by_slug(self, slug: str) -> dict[str, Any] | None:
        with self._runtime.transaction() as transaction:
            record = self._drafts.get_draft_by_slug_for_administration(
                transaction,
                agent_slug=slug,
            )
        return None if record is None else draft_record_to_dict(record)

    def find_gap_draft(
        self,
        user_id: str,
        source_chat_id: str,
        gap_fingerprint: str,
    ) -> dict[str, Any] | None:
        with self._runtime.transaction() as transaction:
            record = self._drafts.find_gap_draft(
                transaction,
                owner_id=user_id,
                source_chat_id=source_chat_id,
                gap_fingerprint=gap_fingerprint,
            )
        return None if record is None else draft_record_to_dict(record)

    def get_user_draft_agents(self, user_id: str) -> list[dict[str, Any]]:
        with self._runtime.transaction() as transaction:
            records = self._drafts.list_drafts(
                transaction,
                owner_id=user_id,
                include_terminal=False,
                limit=2000,
            )
        return [draft_record_to_dict(record) for record in records]

    def get_decidable_drafts(self, user_id: str) -> list[dict[str, Any]]:
        """Return the owner's bounded non-live decision inventory."""

        with self._runtime.transaction() as transaction:
            records = self._drafts.list_drafts(
                transaction,
                owner_id=user_id,
                include_terminal=True,
                limit=2000,
            )
        drafts = [
            draft_record_to_dict(record)
            for record in records
            if record.status != "live"
        ]
        return sorted(
            drafts,
            key=lambda draft: (
                -(int(draft.get("updated_at") or 0)),
                str(draft["id"]),
            ),
        )

    def list_relaunchable_drafts(self) -> list[dict[str, Any]]:
        """Return bounded live server-hosted drafts for boot reconciliation."""

        with self._runtime.transaction() as transaction:
            records = self._drafts.list_drafts_for_administration(
                transaction,
                limit=2000,
            )
        return [
            draft_record_to_dict(record)
            for record in records
            if record.status == "live" and record.origin != "byo_client"
        ]

    def list_byo_sessions(
        self,
        owner_user_id: str,
        *,
        origin: str,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        with self._runtime.transaction() as transaction:
            records = self._drafts.list_drafts(
                transaction,
                owner_id=owner_user_id,
                include_terminal=True,
                origin=origin,
                limit=limit,
            )
        return [draft_record_to_dict(record) for record in records]

    def get_pending_review_drafts(self) -> list[dict[str, Any]]:
        with self._runtime.transaction() as transaction:
            records = self._drafts.list_pending_review_for_administration(
                transaction,
                limit=2000,
            )
        return [draft_record_to_dict(record) for record in records]

    def list_draft_agents(self) -> list[dict[str, Any]]:
        with self._runtime.transaction() as transaction:
            records = self._drafts.list_drafts_for_administration(
                transaction,
                limit=2000,
            )
        return [draft_record_to_dict(record) for record in records]

    def list_expired_draft_generations_for_administration(
        self,
        *,
        limit: int = 100,
        after_generation_claim_expires_at: datetime | None = None,
        after_draft_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return Plane's bounded DB-time inventory of expired generation claims."""

        with self._runtime.transaction() as transaction:
            records = self._drafts.list_expired_generation_claims_for_administration(
                transaction,
                limit=limit,
                after_generation_claim_expires_at=(after_generation_claim_expires_at),
                after_draft_id=after_draft_id,
            )
        return [draft_record_to_dict(record) for record in records]

    def update_draft_agent(self, draft_id: str, **updates: object) -> bool:
        if not updates:
            raise ValueError("draft update must not be empty")
        with self._runtime.transaction() as transaction:
            current = self._drafts.get_draft_for_administration(
                transaction,
                draft_id=draft_id,
                for_update=True,
            )
            if current is None:
                return False
            self._drafts.compare_and_set_draft(
                transaction,
                owner_id=current.owner_id,
                draft_id=draft_id,
                expected_revision=current.state_revision,
                updates=updates,
                updated_at=_now_ms(),
            )
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
        with self._runtime.transaction() as transaction:
            if fenced:
                current = self._drafts.get_draft(
                    transaction,
                    owner_id=owner_user_id,
                    draft_id=draft_id,
                    for_update=True,
                )
            else:
                current = self._drafts.get_draft_for_administration(
                    transaction,
                    draft_id=draft_id,
                    for_update=True,
                )
            if current is None:
                return False
            if fenced and (
                current.owner_id != owner_user_id
                or current.state_revision != expected_revision
                or current.generation_claim_id != claim_id
                or current.status != "generating"
                or current.published_revision_id is not None
            ):
                return False
            claim_owner_id = current.owner_id
            claim_revision = current.state_revision
            active_claim_id = current.generation_claim_id
            if fenced:
                assert owner_user_id is not None
                assert expected_revision is not None
                assert claim_id is not None
                claim_owner_id = owner_user_id
                claim_revision = expected_revision
                active_claim_id = claim_id
            try:
                log = json.loads(current.generation_log or "[]")
            except (TypeError, ValueError):
                log = []
            if not isinstance(log, list):
                log = []
            log.append({"message": message, "timestamp": _now_ms()})
            generation_log = json.dumps(log)
            if active_claim_id is not None:
                # Progress logging is evidence about the claimed generation,
                # not a semantic draft edit. Advancing state_revision here
                # would invalidate the exact revision fence that must later
                # terminalize the claim.
                try:
                    self._drafts.replace_generation_log_for_claim(
                        transaction,
                        owner_id=claim_owner_id,
                        draft_id=draft_id,
                        expected_revision=claim_revision,
                        claim_id=active_claim_id,
                        generation_log=generation_log,
                    )
                except RepositoryConflictError:
                    if fenced:
                        return False
                    raise
            else:
                self._drafts.compare_and_set_draft(
                    transaction,
                    owner_id=current.owner_id,
                    draft_id=draft_id,
                    expected_revision=current.state_revision,
                    updates={"generation_log": generation_log},
                    updated_at=_now_ms(),
                )
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
        try:
            with self._runtime.transaction() as transaction:
                record = self._drafts.claim_generation(
                    transaction,
                    owner_id=owner_user_id,
                    draft_id=draft_id,
                    expected_revision=expected_revision,
                    claim_id=claim_id,
                    lease_seconds=lease_seconds,
                )
        except RepositoryConflictError:
            return None
        return draft_record_to_dict(record)

    def renew_draft_generation(
        self,
        *,
        draft_id: str,
        owner_user_id: str,
        expected_revision: int,
        claim_id: str,
        lease_seconds: int = 300,
    ) -> dict[str, Any] | None:
        """Renew one exact live generation claim without advancing its revision.

        Long-running model calls may outlive the initial lease.  Plane's
        database clock is authoritative for both expiry and renewal, and an
        expired or superseded claim is deliberately returned as a conflict
        instead of being resurrected by product code.
        """

        try:
            with self._runtime.transaction() as transaction:
                record = self._drafts.renew_generation_claim(
                    transaction,
                    owner_id=owner_user_id,
                    draft_id=draft_id,
                    expected_revision=expected_revision,
                    claim_id=claim_id,
                    lease_seconds=lease_seconds,
                )
        except RepositoryConflictError:
            return None
        return draft_record_to_dict(record)

    def get_exact_live_draft_generation_claim(
        self,
        *,
        draft_id: str,
        owner_user_id: str,
        expected_preclaim_revision: int,
        claim_id: str,
    ) -> dict[str, Any] | None:
        """Resolve the exact live post-claim row after acknowledgement loss."""

        with self._runtime.transaction() as transaction:
            record = self._drafts.get_exact_live_generation_claim(
                transaction,
                owner_id=owner_user_id,
                draft_id=draft_id,
                expected_preclaim_revision=expected_preclaim_revision,
                claim_id=claim_id,
            )
        return None if record is None else draft_record_to_dict(record)

    def reclaim_expired_draft_generation(
        self,
        *,
        draft_id: str,
        owner_user_id: str,
        expected_revision: int,
        claim_id: str,
        lease_seconds: int = 300,
    ) -> dict[str, Any] | None:
        """Reselect an expired exact claim and fence the prior worker revision."""

        try:
            with self._runtime.transaction() as transaction:
                record = self._drafts.reclaim_expired_generation_claim(
                    transaction,
                    owner_id=owner_user_id,
                    draft_id=draft_id,
                    expected_revision=expected_revision,
                    claim_id=claim_id,
                    lease_seconds=lease_seconds,
                )
        except RepositoryConflictError:
            return None
        return draft_record_to_dict(record)

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
        try:
            with self._runtime.transaction() as transaction:
                record = self._drafts.finish_generation(
                    transaction,
                    owner_id=owner_user_id,
                    draft_id=draft_id,
                    expected_revision=expected_revision,
                    claim_id=claim_id,
                    status=status,
                    error_message=error_message,
                    security_report=security_report,
                    validation_report=validation_report,
                    required_credentials=required_credentials,
                )
        except RepositoryConflictError:
            return None
        return draft_record_to_dict(record)

    def get_exact_draft_transition(
        self,
        *,
        draft_id: str,
        owner_user_id: str,
        transition_id: str,
        transition_kind: str,
        expected_revision: int,
    ) -> tuple[int, str] | None:
        """Read one exact prior transition without borrowing an observer fence."""

        if (
            not isinstance(draft_id, str)
            or not draft_id
            or not isinstance(owner_user_id, str)
            or not owner_user_id
            or not isinstance(transition_kind, str)
            or not transition_kind
            or type(expected_revision) is not int
            or expected_revision < 0
        ):
            return None
        try:
            transition_id = _canonical_uuid(transition_id, "transition_id")
        except ValueError:
            return None

        # This is deliberately a normal Plane read transaction. The operation
        # fence that authorized the original mutation remains immutable evidence
        # on the transition row; a later observer operation need not impersonate
        # that fence merely to verify the idempotency identity.
        with self._runtime.transaction() as transaction:
            current = self._drafts.get_draft(
                transaction,
                owner_id=owner_user_id,
                draft_id=draft_id,
            )
            replay = self._drafts.get_transition(
                transaction,
                owner_id=owner_user_id,
                transition_id=transition_id,
            )
        if current is None or replay is None or not current.draft_uuid:
            return None

        result_revision = replay.result_revision
        outcome = replay.outcome
        if (
            replay.transition_id != transition_id
            or replay.owner_id != owner_user_id
            or replay.draft_uuid != current.draft_uuid
            or replay.transition_kind != transition_kind
            or replay.expected_revision != expected_revision
            or type(result_revision) is not int
            or result_revision < 0
            or result_revision > current.state_revision
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
        operation_fence: Any | None,
    ) -> tuple[str, int, dict[str, Any]]:
        fenced = transition_id is not None and operation_fence is not None
        with self._transaction(operation_fence if fenced else None) as transaction:
            current = self._drafts.get_draft(
                transaction,
                owner_id=owner_user_id,
                draft_id=draft_id,
                for_update=True,
            )
            if current is None:
                raise LookupError("draft is unavailable")
            if not current.draft_uuid:
                raise RuntimeError("draft UUID alias is missing")

            if fenced:
                replay = self._drafts.get_transition(
                    transaction,
                    owner_id=owner_user_id,
                    transition_id=transition_id,
                )
                if replay is not None:
                    same_identity = (
                        replay.draft_uuid == current.draft_uuid
                        and replay.transition_kind == transition_kind
                        and replay.expected_revision == expected_revision
                        and replay.operation_id == str(operation_fence.operation_id)
                        and replay.operation_execution_generation
                        == operation_fence.execution_generation
                    )
                    if not same_identity:
                        return (
                            "conflict",
                            current.state_revision,
                            draft_record_to_dict(current),
                        )
                    return (
                        "replayed",
                        replay.result_revision,
                        draft_record_to_dict(current),
                    )

            if current.state_revision != expected_revision:
                if fenced:
                    self._drafts.record_transition(
                        transaction,
                        transition_id=transition_id,
                        draft_uuid=current.draft_uuid,
                        owner_id=owner_user_id,
                        operation_id=str(operation_fence.operation_id),
                        operation_execution_generation=(
                            operation_fence.execution_generation
                        ),
                        transition_kind=transition_kind,
                        expected_revision=expected_revision,
                        result_revision=current.state_revision,
                        outcome="conflict",
                        safe_code="stale_revision",
                    )
                return (
                    "conflict",
                    current.state_revision,
                    draft_record_to_dict(current),
                )

            updated = self._drafts.compare_and_set_draft(
                transaction,
                owner_id=owner_user_id,
                draft_id=draft_id,
                expected_revision=expected_revision,
                updates=updates,
                updated_at=_now_ms(),
            )
            if fenced:
                self._drafts.record_transition(
                    transaction,
                    transition_id=transition_id,
                    draft_uuid=current.draft_uuid,
                    owner_id=owner_user_id,
                    operation_id=str(operation_fence.operation_id),
                    operation_execution_generation=(
                        operation_fence.execution_generation
                    ),
                    transition_kind=transition_kind,
                    expected_revision=expected_revision,
                    result_revision=updated.state_revision,
                    outcome="applied",
                )
            return (
                "applied",
                updated.state_revision,
                draft_record_to_dict(updated),
            )

    def delete_draft_agent(self, draft_id: str) -> bool:
        with self._runtime.transaction() as transaction:
            current = self._drafts.get_draft_for_administration(
                transaction,
                draft_id=draft_id,
                for_update=True,
            )
            if current is None:
                return False
            return self._drafts.delete_draft(
                transaction,
                owner_id=current.owner_id,
                draft_id=draft_id,
            )

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self._runtime.transaction() as transaction:
            record = self._identity.get_identity(
                transaction,
                owner_id=user_id,
            )
        if record is None:
            return None
        return {
            "id": record.owner_id,
            "email": record.email,
            "username": record.username,
            "display_name": record.display_name,
            "roles": list(record.roles),
            "last_login_at": record.last_login_at,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    def set_agent_ownership(
        self,
        agent_id: str,
        owner_email: str,
        is_public: bool = False,
    ) -> None:
        with self._runtime.transaction() as transaction:
            self._agents.upsert_ownership(
                transaction,
                agent_id=agent_id,
                owner_email=owner_email,
                is_public=is_public,
                observed_at=_now_ms(),
            )

    def get_agent_ownership(self, agent_id: str) -> dict[str, Any] | None:
        with self._runtime.transaction() as transaction:
            record = self._agents.get_ownership(
                transaction,
                agent_id=agent_id,
            )
        if record is None:
            return None
        return {
            "agent_id": record.agent_id,
            "owner_email": record.owner_email,
            "is_public": record.is_public,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    def set_agent_visibility(self, agent_id: str, is_public: bool) -> bool:
        """Update visibility under the ownership identity locked in Plane."""

        with self._runtime.transaction() as transaction:
            current = self._agents.get_ownership(
                transaction,
                agent_id=agent_id,
            )
            if current is None:
                return False
            self._agents.set_visibility(
                transaction,
                agent_id=agent_id,
                owner_email=current.owner_email,
                is_public=is_public,
                updated_at=_now_ms(),
            )
        return True

    def get_agent_is_safe(self, agent_id: str) -> bool:
        with self._runtime.transaction() as transaction:
            record = self._agents.get_trust(
                transaction,
                agent_id=agent_id,
            )
        return bool(record and record.is_safe)

    def upsert_agent_safe(
        self,
        agent_id: str,
        is_safe: bool,
        *,
        marked_by: str,
    ) -> bool:
        with self._runtime.transaction() as transaction:
            current = self._agents.get_trust(transaction, agent_id=agent_id)
            self._agents.set_trust(
                transaction,
                agent_id=agent_id,
                is_safe=is_safe,
                marked_by=marked_by,
            )
        return bool(current and current.is_safe)

    def reset_agent_safe(self, agent_id: str, *, marked_by: str) -> bool:
        with self._runtime.transaction() as transaction:
            current = self._agents.get_trust(transaction, agent_id=agent_id)
            self._agents.set_trust(
                transaction,
                agent_id=agent_id,
                is_safe=False,
                marked_by=marked_by,
                reset_for_revision=True,
            )
        return bool(current and current.is_safe)

    def purge_agent_state(
        self,
        *,
        owner_user_id: str,
        agent_id: str,
    ) -> int:
        with self._runtime.transaction() as transaction:
            identity = self._identity.get_identity(
                transaction,
                owner_id=owner_user_id,
            )
            removed = self._tool_policy.remove_agent_state(
                transaction,
                owner_id=owner_user_id,
                agent_id=agent_id,
            )
            if identity is not None and identity.email:
                removed += int(
                    self._agents.remove_ownership(
                        transaction,
                        agent_id=agent_id,
                        owner_email=identity.email,
                    )
                )
        return removed

    def list_scoped_agent_owners_for_administration(
        self,
    ) -> tuple[tuple[str, str], ...]:
        with self._runtime.transaction() as transaction:
            records = self._tool_policy.list_scoped_agent_owners_for_administration(
                transaction,
                agent_id_suffix="-1",
                limit=5000,
            )
        return tuple((record.owner_id, record.agent_id) for record in records)


__all__ = ("PlaneDraftStore", "draft_record_to_dict")
