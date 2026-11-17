"""Owner-isolated persistence for draft-agent authoring, generation-claim leases, and
publication records. Used by AstralDeep's orchestrator/draft_plane_store.py and by
repositories/generated_agent_publications.py for its filesystem handoff.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from astralplane.contracts import Transaction
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
    _bounded_limit,
    _bounded_text,
    _non_negative_int,
    _positive_int,
    _required_id,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TRANSITION_KIND = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_SAFE_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_GENERATED_PUBLICATION_MUTATION_CAPABILITY = object()


@dataclass(frozen=True, slots=True)
class DraftAgentRecord:
    draft_id: str
    owner_id: str
    agent_name: str
    agent_slug: str
    description: str
    tools_spec: str | None
    skill_tags: str | None
    packages: str | None
    status: str
    generation_log: str | None
    security_report: str | None
    error_message: str | None
    port: int | None
    review_notes: str | None
    reviewed_by: str | None
    refinement_history: str | None
    validation_report: str | None
    required_credentials: str | None
    origin: str
    source_chat_id: str | None
    gap_fingerprint: str | None
    source_attachment_id: str | None
    revises_agent_id: str | None
    self_test: str | None
    phase: str | None
    clarify_answers: str | None
    plan_json: str | None
    analyze_result: str | None
    constitution_version: str | None
    host_binding: str | None
    draft_uuid: str | None
    target_agent_id: str | None
    state_revision: int
    generation_claim_id: str | None
    generation_claim_expires_at: datetime | None
    published_revision_id: str | None
    created_at: int | None
    updated_at: int | None


@dataclass(frozen=True, slots=True)
class DraftTransitionRecord:
    transition_id: str
    draft_uuid: str
    owner_id: str
    operation_id: str | None
    operation_execution_generation: int
    transition_kind: str
    expected_revision: int
    result_revision: int
    outcome: str
    safe_code: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DraftPublicationRecord:
    publication_id: str
    draft_uuid: str
    owner_id: str
    source_state_revision: int
    generation_claim_id: str
    target_agent_id: str
    target_revision_id: str
    operation_id: str | None
    operation_execution_generation: int | None
    staging_relative_path: str
    revision_relative_path: str
    artifact_digest: str | None
    manifest_digest: str | None
    state: str
    state_revision: int
    created_at: datetime
    published_at: datetime | None
    failed_at: datetime | None
    failure_code: str | None


class DraftAgentRepository:
    def create_draft(
        self,
        transaction: Transaction,
        *,
        draft_id: str,
        owner_id: str,
        agent_name: str,
        agent_slug: str,
        description: str,
        observed_at: int,
        tools_spec: str | None = None,
        skill_tags: str | None = None,
        packages: str | None = None,
        origin: str = "manual",
        source_chat_id: str | None = None,
        gap_fingerprint: str | None = None,
        source_attachment_id: str | None = None,
        revises_agent_id: str | None = None,
        plan_json: str | None = None,
        constitution_version: str | None = None,
        draft_uuid: str | None = None,
        target_agent_id: str | None = None,
    ) -> DraftAgentRecord:
        draft_id = _required_id(draft_id, "draft_id", maximum=512)
        owner_id = _required_id(owner_id, "owner_id")
        agent_name = _bounded_text(agent_name, "agent_name", maximum=1024)
        agent_slug = _bounded_text(agent_slug, "agent_slug", maximum=512)
        description = _bounded_text(description, "description", maximum=100_000, allow_empty=True)
        observed_at = _non_negative_int(observed_at, "observed_at")
        origin = _bounded_text(origin, "origin", maximum=64)
        tools_spec = _optional_text(tools_spec, "tools_spec", 1_000_000)
        skill_tags = _optional_text(skill_tags, "skill_tags", 100_000)
        packages = _optional_text(packages, "packages", 100_000)
        source_chat_id = _optional_text(source_chat_id, "source_chat_id", 512)
        gap_fingerprint = _optional_text(gap_fingerprint, "gap_fingerprint", 512)
        source_attachment_id = _optional_text(source_attachment_id, "source_attachment_id", 512)
        revises_agent_id = _optional_text(revises_agent_id, "revises_agent_id", 512)
        plan_json = _optional_text(plan_json, "plan_json", 1_000_000)
        constitution_version = _optional_text(constitution_version, "constitution_version", 128)
        if draft_uuid is None:
            try:
                draft_uuid = str(uuid.UUID(draft_id))
            except ValueError:
                draft_uuid = str(uuid.uuid4())
        else:
            draft_uuid = _uuid_text(draft_uuid, "draft_uuid")
        target_agent_id = _optional_text(target_agent_id, "target_agent_id", 512)
        if target_agent_id is None:
            target_agent_id = revises_agent_id or str(uuid.uuid4())
        row = transaction.fetch_one(
            """
            INSERT INTO draft_agents (
                id, user_id, agent_name, agent_slug, description, tools_spec,
                skill_tags, packages, status, origin, source_chat_id,
                gap_fingerprint, source_attachment_id, revises_agent_id,
                plan_json, constitution_version, draft_uuid, target_agent_id,
                state_revision, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s,
                %s, %s, %s, %s, %s, %s, %s, 0, %s, %s
            ) ON CONFLICT (id) DO NOTHING RETURNING *
            """,
            (
                draft_id,
                owner_id,
                agent_name,
                agent_slug,
                description,
                tools_spec,
                skill_tags,
                packages,
                origin,
                source_chat_id,
                gap_fingerprint,
                source_attachment_id,
                revises_agent_id,
                plan_json,
                constitution_version,
                draft_uuid,
                target_agent_id,
                observed_at,
                observed_at,
            ),
        )
        if row is None:
            row = transaction.fetch_one(
                "SELECT * FROM draft_agents WHERE id = %s AND user_id = %s",
                (draft_id, owner_id),
            )
        if row is None:
            raise RepositoryConflictError("draft identity is bound to another owner")
        result = _draft(row)
        if (
            result.draft_uuid != draft_uuid
            or result.target_agent_id != target_agent_id
            or result.agent_name != agent_name
            or result.agent_slug != agent_slug
            or result.description != description
            or result.tools_spec != tools_spec
            or result.skill_tags != skill_tags
            or result.packages != packages
            or result.origin != origin
            or result.source_chat_id != source_chat_id
            or result.gap_fingerprint != gap_fingerprint
            or result.source_attachment_id != source_attachment_id
            or result.revises_agent_id != revises_agent_id
            or result.plan_json != plan_json
            or result.constitution_version != constitution_version
        ):
            raise RepositoryConflictError("draft replay changed immutable identities")
        return result

    def bind_attachment_provenance(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        draft_id: str,
        expected_revision: int,
        source_chat_id: str,
        gap_fingerprint: str,
        source_attachment_id: str,
        updated_at: int,
    ) -> DraftAgentRecord:
        owner_id = _required_id(owner_id, "owner_id")
        draft_id = _required_id(draft_id, "draft_id", maximum=512)
        expected_revision = _non_negative_int(expected_revision, "expected_revision")
        source_chat_id = _bounded_text(
            source_chat_id, "source_chat_id", maximum=512, allow_empty=True
        )
        gap_fingerprint = _bounded_text(gap_fingerprint, "gap_fingerprint", maximum=512)
        source_attachment_id = _bounded_text(
            source_attachment_id, "source_attachment_id", maximum=512
        )
        updated_at = _non_negative_int(updated_at, "updated_at")
        row = transaction.fetch_one(
            """
            UPDATE draft_agents SET
                origin = 'auto_attachment', source_chat_id = %s,
                gap_fingerprint = %s, source_attachment_id = %s,
                updated_at = %s, state_revision = state_revision + 1
            WHERE id = %s AND user_id = %s AND state_revision = %s
              AND origin IN ('manual', 'auto_attachment')
              AND source_chat_id IS NULL
              AND gap_fingerprint IS NULL
              AND source_attachment_id IS NULL
            RETURNING *
            """,
            (
                source_chat_id,
                gap_fingerprint,
                source_attachment_id,
                updated_at,
                draft_id,
                owner_id,
                expected_revision,
            ),
        )
        if row is None:
            row = transaction.fetch_one(
                "SELECT * FROM draft_agents WHERE id = %s AND user_id = %s",
                (draft_id, owner_id),
            )
        if row is None:
            raise RepositoryNotFoundError("owner-scoped draft was not found")
        result = _draft(row)
        if (
            result.origin != "auto_attachment"
            or result.source_chat_id != source_chat_id
            or result.gap_fingerprint != gap_fingerprint
            or result.source_attachment_id != source_attachment_id
        ):
            raise RepositoryConflictError("draft attachment provenance fence is stale")
        return result

    def get_draft(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        draft_id: str,
        for_update: bool = False,
    ) -> DraftAgentRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        draft_id = _required_id(draft_id, "draft_id", maximum=512)
        lock = " FOR UPDATE" if for_update else ""
        row = transaction.fetch_one(
            "SELECT * FROM draft_agents WHERE id = %s AND user_id = %s" + lock,
            (draft_id, owner_id),
        )
        return None if row is None else _draft(row)

    def get_draft_by_uuid(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        draft_uuid: str,
        for_update: bool = False,
    ) -> DraftAgentRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        draft_uuid = _uuid_text(draft_uuid, "draft_uuid")
        if not isinstance(for_update, bool):
            raise RepositoryValidationError("for_update must be boolean")
        row = transaction.fetch_one(
            "SELECT * FROM draft_agents WHERE draft_uuid = %s AND user_id = %s"
            + (" FOR UPDATE" if for_update else ""),
            (draft_uuid, owner_id),
        )
        return None if row is None else _draft(row)

    def get_draft_for_administration(
        self,
        transaction: Transaction,
        *,
        draft_id: str,
        for_update: bool = False,
    ) -> DraftAgentRecord | None:
        draft_id = _required_id(draft_id, "draft_id", maximum=512)
        if not isinstance(for_update, bool):
            raise RepositoryValidationError("for_update must be boolean")
        row = transaction.fetch_one(
            "SELECT * FROM draft_agents WHERE id = %s" + (" FOR UPDATE" if for_update else ""),
            (draft_id,),
        )
        return None if row is None else _draft(row)

    def get_draft_by_slug(
        self, transaction: Transaction, *, owner_id: str, agent_slug: str
    ) -> DraftAgentRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        agent_slug = _bounded_text(agent_slug, "agent_slug", maximum=512)
        row = transaction.fetch_one(
            """
            SELECT * FROM draft_agents
            WHERE user_id = %s AND agent_slug = %s
            ORDER BY created_at DESC LIMIT 1
            """,
            (owner_id, agent_slug),
        )
        return None if row is None else _draft(row)

    def get_draft_by_slug_for_administration(
        self,
        transaction: Transaction,
        *,
        agent_slug: str,
    ) -> DraftAgentRecord | None:
        agent_slug = _bounded_text(agent_slug, "agent_slug", maximum=512)
        row = transaction.fetch_one(
            """
            SELECT * FROM draft_agents
            WHERE agent_slug = %s
            ORDER BY created_at DESC NULLS LAST, id ASC
            LIMIT 1
            """,
            (agent_slug,),
        )
        return None if row is None else _draft(row)

    def find_gap_draft(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        source_chat_id: str,
        gap_fingerprint: str,
    ) -> DraftAgentRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        source_chat_id = _bounded_text(
            source_chat_id, "source_chat_id", maximum=512, allow_empty=True
        )
        gap_fingerprint = _bounded_text(gap_fingerprint, "gap_fingerprint", maximum=512)
        row = transaction.fetch_one(
            """
            SELECT * FROM draft_agents
            WHERE user_id = %s AND source_chat_id = %s AND gap_fingerprint = %s
              AND status <> 'live'
            ORDER BY created_at DESC LIMIT 1
            """,
            (owner_id, source_chat_id, gap_fingerprint),
        )
        return None if row is None else _draft(row)

    def list_drafts(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        include_terminal: bool = False,
        origin: str | None = None,
        limit: int = 500,
    ) -> tuple[DraftAgentRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        if not isinstance(include_terminal, bool):
            raise RepositoryValidationError("include_terminal must be boolean")
        origin = _optional_text(origin, "origin", 64)
        limit = _bounded_limit(limit, maximum=2000)
        rows = transaction.fetch_all(
            """
            SELECT * FROM draft_agents
            WHERE user_id = %s
              AND (%s OR status NOT IN ('live', 'rejected'))
              AND (%s IS NULL OR origin = %s)
            ORDER BY created_at DESC, id LIMIT %s
            """,
            (owner_id, include_terminal, origin, origin, limit),
        )
        return tuple(_draft(row) for row in rows)

    def list_pending_review_for_administration(
        self, transaction: Transaction, *, limit: int = 500
    ) -> tuple[DraftAgentRecord, ...]:
        limit = _bounded_limit(limit, maximum=2000)
        rows = transaction.fetch_all(
            """
            SELECT * FROM draft_agents WHERE status = 'pending_review'
            ORDER BY updated_at ASC, id LIMIT %s
            """,
            (limit,),
        )
        return tuple(_draft(row) for row in rows)

    def list_drafts_for_administration(
        self,
        transaction: Transaction,
        *,
        limit: int = 2000,
    ) -> tuple[DraftAgentRecord, ...]:
        limit = _bounded_limit(limit, maximum=2000)
        rows = transaction.fetch_all(
            """
            SELECT * FROM draft_agents
            ORDER BY created_at DESC NULLS LAST, id ASC
            LIMIT %s
            """,
            (limit,),
        )
        return tuple(_draft(row) for row in rows)

    def list_expired_generation_claims_for_administration(
        self,
        transaction: Transaction,
        *,
        limit: int = 100,
        after_generation_claim_expires_at: datetime | None = None,
        after_draft_id: str | None = None,
    ) -> tuple[DraftAgentRecord, ...]:
        limit = _bounded_limit(limit, maximum=1000)
        supplied_cursor = (
            after_generation_claim_expires_at is not None,
            after_draft_id is not None,
        )
        if any(supplied_cursor) and not all(supplied_cursor):
            raise RepositoryValidationError(
                "expired generation claim cursor fields must be supplied together"
            )
        cursor_clause = ""
        parameters: tuple[object, ...] = ()
        if all(supplied_cursor):
            cursor_time = _cursor_time(
                after_generation_claim_expires_at,
                "after_generation_claim_expires_at",
            )
            cursor_draft_id = _required_id(
                after_draft_id,
                "after_draft_id",
                maximum=512,
            )
            cursor_clause = " AND (generation_claim_expires_at, id) > (%s, %s)"
            parameters = (cursor_time, cursor_draft_id)
        rows = transaction.fetch_all(
            f"""
            SELECT * FROM draft_agents
            WHERE status = 'generating'
              AND generation_claim_id IS NOT NULL
              AND generation_claim_expires_at <= clock_timestamp()
              AND published_revision_id IS NULL
              {cursor_clause}
            ORDER BY generation_claim_expires_at ASC, id ASC
            LIMIT %s
            """,
            (*parameters, limit),
        )
        return tuple(_draft(row) for row in rows)

    def compare_and_set_draft(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        draft_id: str,
        expected_revision: int,
        updates: Mapping[str, object],
        updated_at: int,
    ) -> DraftAgentRecord:
        owner_id = _required_id(owner_id, "owner_id")
        draft_id = _required_id(draft_id, "draft_id", maximum=512)
        expected_revision = _non_negative_int(expected_revision, "expected_revision")
        updated_at = _non_negative_int(updated_at, "updated_at")
        assignments, values = _draft_updates(updates)
        if not assignments:
            raise RepositoryValidationError("draft update must contain at least one field")
        row = transaction.fetch_one(
            f"""
            UPDATE draft_agents SET {", ".join(assignments)},
                updated_at = %s, state_revision = state_revision + 1
            WHERE id = %s AND user_id = %s AND state_revision = %s
            RETURNING *
            """,
            (*values, updated_at, draft_id, owner_id, expected_revision),
        )
        if row is None:
            existing = transaction.fetch_one(
                "SELECT state_revision FROM draft_agents WHERE id = %s AND user_id = %s",
                (draft_id, owner_id),
            )
            if existing is None:
                raise RepositoryNotFoundError("owner-scoped draft was not found")
            raise RepositoryConflictError("draft state revision is stale")
        return _draft(row)

    def claim_generation(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        draft_id: str,
        expected_revision: int,
        claim_id: str,
        lease_seconds: int = 300,
    ) -> DraftAgentRecord:
        owner_id = _required_id(owner_id, "owner_id")
        draft_id = _required_id(draft_id, "draft_id", maximum=512)
        expected_revision = _non_negative_int(expected_revision, "expected_revision")
        claim_id = _uuid4_text(claim_id, "claim_id")
        lease_seconds = _positive_int(lease_seconds, "lease_seconds")
        if lease_seconds > 1800:
            raise RepositoryValidationError("lease_seconds must not exceed 1800")
        row = transaction.fetch_one(
            """
            UPDATE draft_agents SET
                generation_claim_id = %s,
                generation_claim_expires_at =
                    clock_timestamp() + (%s * interval '1 second'),
                status = 'generating', error_message = NULL,
                state_revision = state_revision + 1,
                updated_at = (extract(epoch from clock_timestamp()) * 1000)::bigint
            WHERE id = %s AND user_id = %s AND state_revision = %s
              AND (generation_claim_id IS NULL
                   OR generation_claim_expires_at <= clock_timestamp()
                   OR generation_claim_id = %s)
            RETURNING *
            """,
            (claim_id, lease_seconds, draft_id, owner_id, expected_revision, claim_id),
        )
        if row is None:
            raise RepositoryConflictError("draft generation claim fence is stale")
        return _draft(row)

    def get_exact_live_generation_claim(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        draft_id: str,
        expected_preclaim_revision: int,
        claim_id: str,
    ) -> DraftAgentRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        draft_id = _required_id(draft_id, "draft_id", maximum=512)
        expected_preclaim_revision = _non_negative_int(
            expected_preclaim_revision,
            "expected_preclaim_revision",
        )
        claim_id = _uuid4_text(claim_id, "claim_id")
        row = transaction.fetch_one(
            """
            SELECT * FROM draft_agents
            WHERE id = %s AND user_id = %s
              AND state_revision = %s + 1
              AND generation_claim_id = %s
              AND generation_claim_expires_at > clock_timestamp()
              AND status = 'generating' AND published_revision_id IS NULL
            """,
            (
                draft_id,
                owner_id,
                expected_preclaim_revision,
                claim_id,
            ),
        )
        return None if row is None else _draft(row)

    def renew_generation_claim(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        draft_id: str,
        expected_revision: int,
        claim_id: str,
        lease_seconds: int = 300,
    ) -> DraftAgentRecord:
        owner_id = _required_id(owner_id, "owner_id")
        draft_id = _required_id(draft_id, "draft_id", maximum=512)
        expected_revision = _non_negative_int(expected_revision, "expected_revision")
        claim_id = _uuid4_text(claim_id, "claim_id")
        lease_seconds = _positive_int(lease_seconds, "lease_seconds")
        if lease_seconds > 1800:
            raise RepositoryValidationError("lease_seconds must not exceed 1800")
        row = transaction.fetch_one(
            """
            UPDATE draft_agents SET
                generation_claim_expires_at =
                    GREATEST(
                        generation_claim_expires_at,
                        clock_timestamp() + (%s * interval '1 second')
                    ),
                updated_at =
                    (extract(epoch from clock_timestamp()) * 1000)::bigint
            WHERE id = %s AND user_id = %s AND state_revision = %s
              AND generation_claim_id = %s
              AND generation_claim_expires_at > clock_timestamp()
              AND status = 'generating' AND published_revision_id IS NULL
            RETURNING *
            """,
            (
                lease_seconds,
                draft_id,
                owner_id,
                expected_revision,
                claim_id,
            ),
        )
        if row is None:
            raise RepositoryConflictError("draft generation claim renewal fence is stale")
        renewed = _draft(row)
        if renewed.state_revision != expected_revision:
            raise RepositoryDataError(
                "draft generation claim renewal changed the lifecycle revision"
            )
        return renewed

    def reclaim_expired_generation_claim(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        draft_id: str,
        expected_revision: int,
        claim_id: str,
        lease_seconds: int = 300,
    ) -> DraftAgentRecord:
        owner_id = _required_id(owner_id, "owner_id")
        draft_id = _required_id(draft_id, "draft_id", maximum=512)
        expected_revision = _non_negative_int(expected_revision, "expected_revision")
        claim_id = _uuid4_text(claim_id, "claim_id")
        lease_seconds = _positive_int(lease_seconds, "lease_seconds")
        if lease_seconds > 1800:
            raise RepositoryValidationError("lease_seconds must not exceed 1800")
        row = transaction.fetch_one(
            """
            UPDATE draft_agents SET
                generation_claim_expires_at =
                    clock_timestamp() + (%s * interval '1 second'),
                state_revision = state_revision + 1,
                updated_at =
                    (extract(epoch from clock_timestamp()) * 1000)::bigint
            WHERE id = %s AND user_id = %s AND state_revision = %s
              AND generation_claim_id = %s
              AND generation_claim_expires_at <= clock_timestamp()
              AND status = 'generating' AND published_revision_id IS NULL
            RETURNING *
            """,
            (
                lease_seconds,
                draft_id,
                owner_id,
                expected_revision,
                claim_id,
            ),
        )
        if row is None:
            raise RepositoryConflictError("draft generation claim reclaim fence is stale")
        reclaimed = _draft(row)
        if (
            reclaimed.state_revision != expected_revision + 1
            or reclaimed.generation_claim_id != claim_id
            or reclaimed.generation_claim_expires_at is None
            or reclaimed.status != "generating"
            or reclaimed.published_revision_id is not None
        ):
            raise RepositoryDataError("draft generation claim reclaim returned invalid state")
        return reclaimed

    def replace_generation_log_for_claim(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        draft_id: str,
        expected_revision: int,
        claim_id: str,
        generation_log: str,
    ) -> DraftAgentRecord:
        owner_id = _required_id(owner_id, "owner_id")
        draft_id = _required_id(draft_id, "draft_id", maximum=512)
        expected_revision = _non_negative_int(expected_revision, "expected_revision")
        claim_id = _uuid4_text(claim_id, "claim_id")
        generation_log = _bounded_text(
            generation_log,
            "generation_log",
            maximum=1_048_576,
            allow_empty=True,
        )
        row = transaction.fetch_one(
            """
            UPDATE draft_agents SET generation_log = %s
            WHERE id = %s AND user_id = %s AND state_revision = %s
              AND generation_claim_id = %s
              AND generation_claim_expires_at > clock_timestamp()
            RETURNING *
            """,
            (generation_log, draft_id, owner_id, expected_revision, claim_id),
        )
        if row is None:
            raise RepositoryConflictError("draft generation log claim fence is stale")
        result = _draft(row)
        if result.state_revision != expected_revision:
            raise RepositoryDataError("draft generation log update changed the lifecycle revision")
        return result

    def finish_generation(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        draft_id: str,
        expected_revision: int,
        claim_id: str,
        status: str,
        error_message: str | None = None,
        security_report: str | None = None,
        validation_report: str | None = None,
        required_credentials: str | None = None,
    ) -> DraftAgentRecord:
        if status not in {"generated", "error"}:
            raise RepositoryValidationError("generation terminal status is invalid")
        owner_id = _required_id(owner_id, "owner_id")
        draft_id = _required_id(draft_id, "draft_id", maximum=512)
        expected_revision = _non_negative_int(expected_revision, "expected_revision")
        claim_id = _uuid4_text(claim_id, "claim_id")
        row = transaction.fetch_one(
            """
            UPDATE draft_agents SET
                generation_claim_id = NULL, generation_claim_expires_at = NULL,
                status = %s, error_message = %s, security_report = %s,
                validation_report = %s, required_credentials = %s,
                state_revision = state_revision + 1,
                updated_at = (extract(epoch from clock_timestamp()) * 1000)::bigint
            WHERE id = %s AND user_id = %s AND state_revision = %s
              AND generation_claim_id = %s
              AND generation_claim_expires_at > clock_timestamp()
            RETURNING *
            """,
            (
                status,
                error_message,
                security_report,
                validation_report,
                required_credentials,
                draft_id,
                owner_id,
                expected_revision,
                claim_id,
            ),
        )
        if row is None:
            raise RepositoryConflictError("draft generation completion fence is stale")
        return _draft(row)

    def delete_draft(self, transaction: Transaction, *, owner_id: str, draft_id: str) -> bool:
        owner_id = _required_id(owner_id, "owner_id")
        draft_id = _required_id(draft_id, "draft_id", maximum=512)
        row = transaction.fetch_one(
            "DELETE FROM draft_agents WHERE id = %s AND user_id = %s RETURNING id",
            (draft_id, owner_id),
        )
        return row is not None

    def record_transition(
        self,
        transaction: Transaction,
        *,
        transition_id: str,
        draft_uuid: str,
        owner_id: str,
        operation_execution_generation: int,
        transition_kind: str,
        expected_revision: int,
        result_revision: int,
        outcome: str,
        operation_id: str | None = None,
        safe_code: str | None = None,
    ) -> DraftTransitionRecord:
        transition_id = _uuid_text(transition_id, "transition_id")
        draft_uuid = _uuid_text(draft_uuid, "draft_uuid")
        owner_id = _required_id(owner_id, "owner_id")
        operation_id = _optional_uuid(operation_id, "operation_id")
        operation_execution_generation = _positive_int(
            operation_execution_generation, "operation_execution_generation"
        )
        if (
            not isinstance(transition_kind, str)
            or _TRANSITION_KIND.fullmatch(transition_kind) is None
        ):
            raise RepositoryValidationError("transition_kind is invalid")
        expected_revision = _non_negative_int(expected_revision, "expected_revision")
        result_revision = _non_negative_int(result_revision, "result_revision")
        if outcome not in {"applied", "conflict", "failed", "replayed"}:
            raise RepositoryValidationError("transition outcome is invalid")
        safe_code = _optional_text(safe_code, "safe_code", 256)
        row = transaction.fetch_one(
            """
            INSERT INTO draft_transition (
                transition_id, draft_uuid, owner_user_id, operation_id,
                operation_execution_generation, transition_kind,
                expected_revision, result_revision, outcome, safe_code
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (transition_id) DO NOTHING RETURNING *
            """,
            (
                transition_id,
                draft_uuid,
                owner_id,
                operation_id,
                operation_execution_generation,
                transition_kind,
                expected_revision,
                result_revision,
                outcome,
                safe_code,
            ),
        )
        if row is None:
            row = transaction.fetch_one(
                """
                SELECT * FROM draft_transition
                WHERE transition_id = %s AND owner_user_id = %s
                """,
                (transition_id, owner_id),
            )
        if row is None:
            raise RepositoryConflictError("draft transition identity conflicts")
        result = _transition(row)
        if (
            result.draft_uuid != draft_uuid
            or result.operation_id != operation_id
            or result.operation_execution_generation != operation_execution_generation
            or result.transition_kind != transition_kind
            or result.expected_revision != expected_revision
            or result.result_revision != result_revision
            or result.outcome != outcome
            or result.safe_code != safe_code
        ):
            raise RepositoryConflictError("draft transition replay changed semantics")
        return result

    def get_transition(
        self, transaction: Transaction, *, owner_id: str, transition_id: str
    ) -> DraftTransitionRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        transition_id = _uuid_text(transition_id, "transition_id")
        row = transaction.fetch_one(
            """
            SELECT * FROM draft_transition
            WHERE transition_id = %s AND owner_user_id = %s
            """,
            (transition_id, owner_id),
        )
        return None if row is None else _transition(row)

    def _create_publication(
        self,
        transaction: Transaction,
        *,
        _capability: object,
        publication_id: str,
        draft_uuid: str,
        owner_id: str,
        source_state_revision: int,
        generation_claim_id: str,
        target_agent_id: str,
        target_revision_id: str,
        staging_relative_path: str,
        revision_relative_path: str,
        operation_id: str | None = None,
        operation_execution_generation: int | None = None,
    ) -> DraftPublicationRecord:
        if _capability is not _GENERATED_PUBLICATION_MUTATION_CAPABILITY:
            raise RepositoryValidationError("publication mutation capability is required")
        publication_id = _uuid_text(publication_id, "publication_id")
        draft_uuid = _uuid_text(draft_uuid, "draft_uuid")
        owner_id = _required_id(owner_id, "owner_id")
        source_state_revision = _non_negative_int(source_state_revision, "source_state_revision")
        generation_claim_id = _uuid_text(generation_claim_id, "generation_claim_id")
        target_agent_id = _required_id(target_agent_id, "target_agent_id", maximum=255)
        if _SAFE_PATH_COMPONENT.fullmatch(target_agent_id) is None or target_agent_id in {
            ".",
            "..",
        }:
            raise RepositoryValidationError(
                "target_agent_id must be a safe publication path component"
            )
        target_revision_id = _uuid_text(target_revision_id, "target_revision_id")
        staging_relative_path = _relative_path(staging_relative_path, "staging_relative_path")
        revision_relative_path = _relative_path(revision_relative_path, "revision_relative_path")
        if staging_relative_path != (
            f"staging/{draft_uuid}/{source_state_revision}/{publication_id}"
        ) or revision_relative_path != (f"revisions/{target_agent_id}/{target_revision_id}"):
            raise RepositoryValidationError(
                "publication paths are not canonical for their stored identity"
            )
        operation_id = _optional_uuid(operation_id, "operation_id")
        if operation_execution_generation is not None:
            operation_execution_generation = _positive_int(
                operation_execution_generation, "operation_execution_generation"
            )
        row = transaction.fetch_one(
            """
            INSERT INTO draft_artifact_publication (
                publication_id, draft_uuid, owner_user_id, source_state_revision,
                generation_claim_id, target_agent_id, target_revision_id,
                operation_id, operation_execution_generation,
                staging_relative_path, revision_relative_path, state
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'claimed')
            ON CONFLICT (publication_id) DO NOTHING RETURNING *
            """,
            (
                publication_id,
                draft_uuid,
                owner_id,
                source_state_revision,
                generation_claim_id,
                target_agent_id,
                target_revision_id,
                operation_id,
                operation_execution_generation,
                staging_relative_path,
                revision_relative_path,
            ),
        )
        if row is None:
            row = transaction.fetch_one(
                """
                SELECT * FROM draft_artifact_publication
                WHERE publication_id = %s AND owner_user_id = %s
                """,
                (publication_id, owner_id),
            )
        if row is None:
            raise RepositoryConflictError("draft publication identity conflicts")
        result = _publication(row)
        if (
            result.draft_uuid != draft_uuid
            or result.source_state_revision != source_state_revision
            or result.generation_claim_id != generation_claim_id
            or result.target_agent_id != target_agent_id
            or result.target_revision_id != target_revision_id
            or result.operation_id != operation_id
            or result.operation_execution_generation != operation_execution_generation
            or result.staging_relative_path != staging_relative_path
            or result.revision_relative_path != revision_relative_path
        ):
            raise RepositoryConflictError("draft publication replay changed semantics")
        return result

    def get_publication(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        publication_id: str,
        for_update: bool = False,
    ) -> DraftPublicationRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        publication_id = _uuid_text(publication_id, "publication_id")
        if not isinstance(for_update, bool):
            raise RepositoryValidationError("for_update must be boolean")
        row = transaction.fetch_one(
            """
            SELECT * FROM draft_artifact_publication
            WHERE publication_id = %s AND owner_user_id = %s
            """
            + (" FOR UPDATE" if for_update else ""),
            (publication_id, owner_id),
        )
        return None if row is None else _publication(row)

    def get_publication_by_source(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        draft_uuid: str,
        source_state_revision: int,
        for_update: bool = False,
    ) -> DraftPublicationRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        draft_uuid = _uuid_text(draft_uuid, "draft_uuid")
        source_state_revision = _non_negative_int(source_state_revision, "source_state_revision")
        if not isinstance(for_update, bool):
            raise RepositoryValidationError("for_update must be boolean")
        row = transaction.fetch_one(
            """
            SELECT * FROM draft_artifact_publication
            WHERE owner_user_id = %s AND draft_uuid = %s
              AND source_state_revision = %s
            """
            + (" FOR UPDATE" if for_update else ""),
            (owner_id, draft_uuid, source_state_revision),
        )
        return None if row is None else _publication(row)

    def get_publication_by_target_revision(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        target_agent_id: str,
        target_revision_id: str,
        for_update: bool = False,
    ) -> DraftPublicationRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        target_agent_id = _required_id(target_agent_id, "target_agent_id", maximum=512)
        target_revision_id = _uuid_text(target_revision_id, "target_revision_id")
        if not isinstance(for_update, bool):
            raise RepositoryValidationError("for_update must be boolean")
        row = transaction.fetch_one(
            """
            SELECT * FROM draft_artifact_publication
            WHERE owner_user_id = %s AND target_agent_id = %s
              AND target_revision_id = %s
            """
            + (" FOR UPDATE" if for_update else ""),
            (owner_id, target_agent_id, target_revision_id),
        )
        return None if row is None else _publication(row)

    def list_reconcilable_publications_for_administration(
        self,
        transaction: Transaction,
        *,
        limit: int = 100,
        after_created_at: datetime | None = None,
        after_publication_id: str | None = None,
    ) -> tuple[DraftPublicationRecord, ...]:
        limit = _bounded_limit(limit, maximum=1000)
        supplied_cursor = (
            after_created_at is not None,
            after_publication_id is not None,
        )
        if any(supplied_cursor) and not all(supplied_cursor):
            raise RepositoryValidationError(
                "publication inventory cursor fields must be supplied together"
            )
        cursor_clause = ""
        parameters: tuple[object, ...] = ()
        if all(supplied_cursor):
            cursor_time = _cursor_time(after_created_at, "after_created_at")
            cursor_publication_text = _required_id(
                after_publication_id,
                "after_publication_id",
                maximum=36,
            )
            cursor_publication_id = _uuid_text(
                cursor_publication_text,
                "after_publication_id",
            )
            cursor_clause = " AND (created_at, publication_id) > (%s, %s)"
            parameters = (cursor_time, cursor_publication_id)
        rows = transaction.fetch_all(
            f"""
            SELECT * FROM draft_artifact_publication
            WHERE state IN ('claimed', 'staged', 'validated')
              {cursor_clause}
            ORDER BY created_at, publication_id
            LIMIT %s
            """,
            (*parameters, limit),
        )
        return tuple(_publication(row) for row in rows)

    def _transition_publication(
        self,
        transaction: Transaction,
        *,
        _capability: object,
        owner_id: str,
        publication_id: str,
        expected_revision: int,
        expected_state: str,
        updates: Mapping[str, object],
    ) -> DraftPublicationRecord:
        if _capability is not _GENERATED_PUBLICATION_MUTATION_CAPABILITY:
            raise RepositoryValidationError("publication mutation capability is required")
        owner_id = _required_id(owner_id, "owner_id")
        publication_id = _uuid_text(publication_id, "publication_id")
        expected_revision = _non_negative_int(expected_revision, "expected_revision")
        expected_state = _bounded_text(expected_state, "expected_state", maximum=64)
        assignments, values = _publication_updates(updates)
        if not assignments:
            raise RepositoryValidationError("publication transition must update a field")
        row = transaction.fetch_one(
            f"""
            UPDATE draft_artifact_publication SET {", ".join(assignments)},
                state_revision = state_revision + 1
            WHERE publication_id = %s AND owner_user_id = %s
              AND state_revision = %s AND state = %s
            RETURNING *
            """,
            (*values, publication_id, owner_id, expected_revision, expected_state),
        )
        if row is None:
            raise RepositoryConflictError("draft publication state fence is stale")
        return _publication(row)


_DRAFT_COLUMNS = {
    "agent_name",
    "agent_slug",
    "analyze_result",
    "clarify_answers",
    "constitution_version",
    "description",
    "error_message",
    "generation_log",
    "host_binding",
    "packages",
    "phase",
    "plan_json",
    "port",
    "published_revision_id",
    "refinement_history",
    "required_credentials",
    "review_notes",
    "reviewed_by",
    "security_report",
    "self_test",
    "skill_tags",
    "status",
    "tools_spec",
    "validation_report",
}
_PUBLICATION_COLUMNS = {
    "artifact_digest",
    "failed_at",
    "failure_code",
    "manifest_digest",
    "published_at",
    "state",
}


def _draft_updates(updates: Mapping[str, object]) -> tuple[list[str], list[object]]:
    if not isinstance(updates, Mapping):
        raise RepositoryValidationError("draft updates must be a mapping")
    unknown = set(updates) - _DRAFT_COLUMNS
    if unknown:
        raise RepositoryValidationError(
            "draft update contains unsupported fields",
            metadata={"fields": ",".join(sorted(unknown))},
        )
    normalized = dict(updates)
    if "published_revision_id" in normalized:
        normalized["published_revision_id"] = _optional_uuid(
            normalized["published_revision_id"], "published_revision_id"
        )
    if "port" in normalized and normalized["port"] is not None:
        normalized["port"] = _positive_int(normalized["port"], "port")
        if int(normalized["port"]) > 65_535:
            raise RepositoryValidationError("port must not exceed 65535")
    assignments = [f"{name} = %s" for name in sorted(normalized)]
    return assignments, [normalized[name] for name in sorted(normalized)]


def _publication_updates(
    updates: Mapping[str, object],
) -> tuple[list[str], list[object]]:
    if not isinstance(updates, Mapping):
        raise RepositoryValidationError("publication updates must be a mapping")
    unknown = set(updates) - _PUBLICATION_COLUMNS
    if unknown:
        raise RepositoryValidationError(
            "publication update contains unsupported fields",
            metadata={"fields": ",".join(sorted(unknown))},
        )
    normalized = dict(updates)
    for name in ("artifact_digest", "manifest_digest"):
        if name in normalized:
            normalized[name] = _optional_digest(normalized[name], name)
    assignments = [f"{name} = %s" for name in sorted(normalized)]
    return assignments, [normalized[name] for name in sorted(normalized)]


def _optional_text(value: object, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field, maximum=maximum, allow_empty=True)


def _uuid_text(value: object, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as exc:
        raise RepositoryValidationError(f"{field} must be a UUID") from exc


def _uuid4_text(value: object, field: str) -> str:
    parsed = uuid.UUID(_uuid_text(value, field))
    if parsed.version != 4:
        raise RepositoryValidationError(f"{field} must be a UUID4")
    return str(parsed)


def _optional_uuid(value: object, field: str) -> str | None:
    return None if value is None else _uuid_text(value, field)


def _optional_digest(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise RepositoryValidationError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _relative_path(value: object, field: str) -> str:
    path = _bounded_text(value, field, maximum=2048)
    parts = path.split("/")
    if (
        "\\" in path
        or path.startswith("/")
        or _WINDOWS_DRIVE.match(path) is not None
        or any(part in {"", ".", ".."} for part in parts)
        or "/".join(parts) != path
    ):
        raise RepositoryValidationError(f"{field} must be canonical POSIX-relative")
    return path


def _aware_time(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RepositoryDataError(f"persisted {field} must be timezone aware")
    return value


def _cursor_time(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RepositoryValidationError(f"{field} must be timezone aware")
    return value


def _optional_time(value: object, field: str) -> datetime | None:
    return None if value is None else _aware_time(value, field)


def _draft(row: Mapping[str, Any]) -> DraftAgentRecord:
    return DraftAgentRecord(
        draft_id=str(row["id"]),
        owner_id=str(row["user_id"]),
        agent_name=str(row["agent_name"]),
        agent_slug=str(row["agent_slug"]),
        description=str(row["description"]),
        tools_spec=_row_text(row, "tools_spec"),
        skill_tags=_row_text(row, "skill_tags"),
        packages=_row_text(row, "packages"),
        status=str(row["status"]),
        generation_log=_row_text(row, "generation_log"),
        security_report=_row_text(row, "security_report"),
        error_message=_row_text(row, "error_message"),
        port=None if row.get("port") is None else int(row["port"]),
        review_notes=_row_text(row, "review_notes"),
        reviewed_by=_row_text(row, "reviewed_by"),
        refinement_history=_row_text(row, "refinement_history"),
        validation_report=_row_text(row, "validation_report"),
        required_credentials=_row_text(row, "required_credentials"),
        origin=str(row.get("origin") or "manual"),
        source_chat_id=_row_text(row, "source_chat_id"),
        gap_fingerprint=_row_text(row, "gap_fingerprint"),
        source_attachment_id=_row_text(row, "source_attachment_id"),
        revises_agent_id=_row_text(row, "revises_agent_id"),
        self_test=_row_text(row, "self_test"),
        phase=_row_text(row, "phase"),
        clarify_answers=_row_text(row, "clarify_answers"),
        plan_json=_row_text(row, "plan_json"),
        analyze_result=_row_text(row, "analyze_result"),
        constitution_version=_row_text(row, "constitution_version"),
        host_binding=_row_text(row, "host_binding"),
        draft_uuid=_row_text(row, "draft_uuid"),
        target_agent_id=_row_text(row, "target_agent_id"),
        state_revision=int(row.get("state_revision", 0)),
        generation_claim_id=_row_text(row, "generation_claim_id"),
        generation_claim_expires_at=_optional_time(
            row.get("generation_claim_expires_at"), "generation_claim_expires_at"
        ),
        published_revision_id=_row_text(row, "published_revision_id"),
        created_at=None if row.get("created_at") is None else int(row["created_at"]),
        updated_at=None if row.get("updated_at") is None else int(row["updated_at"]),
    )


def _transition(row: Mapping[str, Any]) -> DraftTransitionRecord:
    return DraftTransitionRecord(
        transition_id=str(row["transition_id"]),
        draft_uuid=str(row["draft_uuid"]),
        owner_id=str(row["owner_user_id"]),
        operation_id=_row_text(row, "operation_id"),
        operation_execution_generation=int(row["operation_execution_generation"]),
        transition_kind=str(row["transition_kind"]),
        expected_revision=int(row["expected_revision"]),
        result_revision=int(row["result_revision"]),
        outcome=str(row["outcome"]),
        safe_code=_row_text(row, "safe_code"),
        created_at=_aware_time(row["created_at"], "created_at"),
    )


def _publication(row: Mapping[str, Any]) -> DraftPublicationRecord:
    return DraftPublicationRecord(
        publication_id=str(row["publication_id"]),
        draft_uuid=str(row["draft_uuid"]),
        owner_id=str(row["owner_user_id"]),
        source_state_revision=int(row["source_state_revision"]),
        generation_claim_id=str(row["generation_claim_id"]),
        target_agent_id=str(row["target_agent_id"]),
        target_revision_id=str(row["target_revision_id"]),
        operation_id=_row_text(row, "operation_id"),
        operation_execution_generation=(
            None
            if row.get("operation_execution_generation") is None
            else int(row["operation_execution_generation"])
        ),
        staging_relative_path=str(row["staging_relative_path"]),
        revision_relative_path=str(row["revision_relative_path"]),
        artifact_digest=_row_text(row, "artifact_digest"),
        manifest_digest=_row_text(row, "manifest_digest"),
        state=str(row["state"]),
        state_revision=int(row["state_revision"]),
        created_at=_aware_time(row["created_at"], "created_at"),
        published_at=_optional_time(row.get("published_at"), "published_at"),
        failed_at=_optional_time(row.get("failed_at"), "failed_at"),
        failure_code=_row_text(row, "failure_code"),
    )


def _row_text(row: Mapping[str, Any], field: str) -> str | None:
    return None if row.get(field) is None else str(row[field])


__all__ = (
    "DraftAgentRecord",
    "DraftAgentRepository",
    "DraftPublicationRecord",
    "DraftTransitionRecord",
)
