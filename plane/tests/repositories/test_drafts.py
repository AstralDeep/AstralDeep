"""Tests for astralplane.repositories.drafts: owner-scoped draft CRUD, generation-claim
lease renewal and expiry recovery, attachment provenance fencing, and
transition/publication replay semantics.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from _support import ScriptedTransaction

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.drafts import (
    _GENERATED_PUBLICATION_MUTATION_CAPABILITY,
    DraftAgentRepository,
)

NOW = datetime(2026, 8, 14, tzinfo=UTC)
DRAFT_UUID = str(uuid.UUID("10000000-0000-4000-8000-000000000001"))
TARGET_REVISION = str(uuid.UUID("20000000-0000-4000-8000-000000000001"))
CLAIM = str(uuid.UUID("30000000-0000-4000-8000-000000000001"))
TRANSITION = str(uuid.UUID("40000000-0000-4000-8000-000000000001"))
PUBLICATION = str(uuid.UUID("50000000-0000-4000-8000-000000000001"))
DIGEST = "a" * 64
STAGING_PATH = f"staging/{DRAFT_UUID}/1/{PUBLICATION}"
REVISION_PATH = f"revisions/agent-1/{TARGET_REVISION}"


def draft_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "draft-1",
        "user_id": "owner-1",
        "agent_name": "Research Agent",
        "agent_slug": "research-agent",
        "description": "Draft",
        "tools_spec": None,
        "skill_tags": None,
        "packages": None,
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
        "origin": "manual",
        "source_chat_id": None,
        "gap_fingerprint": None,
        "source_attachment_id": None,
        "revises_agent_id": None,
        "self_test": None,
        "phase": None,
        "clarify_answers": None,
        "plan_json": None,
        "analyze_result": None,
        "constitution_version": None,
        "host_binding": None,
        "draft_uuid": DRAFT_UUID,
        "target_agent_id": "agent-1",
        "state_revision": 0,
        "generation_claim_id": None,
        "generation_claim_expires_at": None,
        "published_revision_id": None,
        "created_at": 1,
        "updated_at": 1,
    }
    row.update(changes)
    return row


def transition_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "transition_id": TRANSITION,
        "draft_uuid": DRAFT_UUID,
        "owner_user_id": "owner-1",
        "operation_id": None,
        "operation_execution_generation": 1,
        "transition_kind": "advance_phase",
        "expected_revision": 0,
        "result_revision": 1,
        "outcome": "applied",
        "safe_code": None,
        "created_at": NOW,
    }
    row.update(changes)
    return row


def publication_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "publication_id": PUBLICATION,
        "draft_uuid": DRAFT_UUID,
        "owner_user_id": "owner-1",
        "source_state_revision": 1,
        "generation_claim_id": CLAIM,
        "target_agent_id": "agent-1",
        "target_revision_id": TARGET_REVISION,
        "operation_id": None,
        "operation_execution_generation": 1,
        "staging_relative_path": STAGING_PATH,
        "revision_relative_path": REVISION_PATH,
        "artifact_digest": None,
        "manifest_digest": None,
        "state": "claimed",
        "state_revision": 0,
        "created_at": NOW,
        "published_at": None,
        "failed_at": None,
        "failure_code": None,
    }
    row.update(changes)
    return row


def test_create_and_reads_are_owner_scoped_with_immutable_uuid_aliases() -> None:
    repository = DraftAgentRepository()
    created = repository.create_draft(
        ScriptedTransaction(one=[draft_row()]),
        draft_id="draft-1",
        owner_id="owner-1",
        agent_name="Research Agent",
        agent_slug="research-agent",
        description="Draft",
        observed_at=1,
        draft_uuid=DRAFT_UUID,
        target_agent_id="agent-1",
    )
    assert created.draft_uuid == DRAFT_UUID
    transaction = ScriptedTransaction(one=[draft_row()])
    assert repository.get_draft(transaction, owner_id="owner-1", draft_id="draft-1") == created
    assert transaction.calls[0][2] == ("draft-1", "owner-1")

    admin = ScriptedTransaction(one=[draft_row()])
    assert (
        repository.get_draft_for_administration(
            admin,
            draft_id="draft-1",
            for_update=True,
        ).draft_id
        == "draft-1"
    )  # type: ignore[union-attr]
    assert admin.calls[0][2] == ("draft-1",)
    assert "FOR UPDATE" in admin.fetch_sql()


def test_create_draft_persists_initial_plan_and_constitution_atomically() -> None:
    repository = DraftAgentRepository()
    plan_json = '{"tasks":["verify provenance"]}'
    transaction = ScriptedTransaction(
        one=[
            draft_row(
                plan_json=plan_json,
                constitution_version="0.1.0",
            )
        ]
    )

    created = repository.create_draft(
        transaction,
        draft_id="draft-1",
        owner_id="owner-1",
        agent_name="Research Agent",
        agent_slug="research-agent",
        description="Draft",
        observed_at=1,
        plan_json=plan_json,
        constitution_version="0.1.0",
        draft_uuid=DRAFT_UUID,
        target_agent_id="agent-1",
    )

    assert created.plan_json == plan_json
    assert created.constitution_version == "0.1.0"
    assert len(transaction.calls) == 1
    statement = transaction.fetch_sql()
    assert "plan_json, constitution_version, draft_uuid" in statement
    parameters = transaction.calls[0][2]
    assert isinstance(parameters, tuple)
    assert parameters[13:15] == (plan_json, "0.1.0")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("plan_json", object(), "plan_json must be a string"),
        ("plan_json", "x" * 1_000_001, "plan_json exceeds its maximum length"),
        (
            "constitution_version",
            object(),
            "constitution_version must be a string",
        ),
        (
            "constitution_version",
            "x" * 129,
            "constitution_version exceeds its maximum length",
        ),
    ),
    ids=(
        "plan-not-text",
        "plan-oversized",
        "constitution-not-text",
        "constitution-oversized",
    ),
)
def test_create_draft_rejects_malformed_or_oversized_initial_provenance(
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(RepositoryValidationError, match=message):
        DraftAgentRepository().create_draft(
            ScriptedTransaction(),
            draft_id="draft-1",
            owner_id="owner-1",
            agent_name="Research Agent",
            agent_slug="research-agent",
            description="Draft",
            observed_at=1,
            draft_uuid=DRAFT_UUID,
            target_agent_id="agent-1",
            **{field: value},
        )


def test_draft_update_and_generation_claim_use_owner_revision_and_lease_fences() -> None:
    repository = DraftAgentRepository()
    update_transaction = ScriptedTransaction(
        one=[draft_row(status="analyzing", state_revision=1, updated_at=2)]
    )
    updated = repository.compare_and_set_draft(
        update_transaction,
        owner_id="owner-1",
        draft_id="draft-1",
        expected_revision=0,
        updates={"status": "analyzing"},
        updated_at=2,
    )
    assert updated.state_revision == 1
    assert "user_id = %s AND state_revision = %s" in update_transaction.fetch_sql()

    claim_transaction = ScriptedTransaction(
        one=[
            draft_row(
                status="generating",
                generation_claim_id=CLAIM,
                generation_claim_expires_at=NOW + timedelta(minutes=5),
                state_revision=2,
            )
        ]
    )
    claimed = repository.claim_generation(
        claim_transaction,
        owner_id="owner-1",
        draft_id="draft-1",
        expected_revision=1,
        claim_id=CLAIM,
    )
    assert claimed.generation_claim_id == CLAIM
    assert "clock_timestamp()" in claim_transaction.fetch_sql()
    with pytest.raises(RepositoryValidationError):
        repository.claim_generation(
            ScriptedTransaction(),
            owner_id="owner-1",
            draft_id="draft-1",
            expected_revision=1,
            claim_id=CLAIM,
            lease_seconds=1801,
        )


def test_generation_log_replace_preserves_the_active_claim_revision() -> None:
    repository = DraftAgentRepository()
    transaction = ScriptedTransaction(
        one=[
            draft_row(
                status="generating",
                generation_log='[{"message":"progress"}]',
                generation_claim_id=CLAIM,
                generation_claim_expires_at=NOW + timedelta(minutes=5),
                state_revision=2,
            )
        ]
    )
    updated = repository.replace_generation_log_for_claim(
        transaction,
        owner_id="owner-1",
        draft_id="draft-1",
        expected_revision=2,
        claim_id=CLAIM,
        generation_log='[{"message":"progress"}]',
    )

    assert updated.state_revision == 2
    assert updated.generation_log == '[{"message":"progress"}]'
    statement = transaction.fetch_sql()
    assert "generation_claim_expires_at > clock_timestamp()" in statement
    assert "state_revision = state_revision + 1" not in statement
    assert transaction.calls[0][2] == (
        '[{"message":"progress"}]',
        "draft-1",
        "owner-1",
        2,
        CLAIM,
    )

    with pytest.raises(RepositoryConflictError, match="log claim fence"):
        repository.replace_generation_log_for_claim(
            ScriptedTransaction(one=[None]),
            owner_id="owner-1",
            draft_id="draft-1",
            expected_revision=2,
            claim_id=CLAIM,
            generation_log="[]",
        )
    with pytest.raises(RepositoryDataError, match="changed the lifecycle revision"):
        repository.replace_generation_log_for_claim(
            ScriptedTransaction(
                one=[
                    draft_row(
                        status="generating",
                        generation_claim_id=CLAIM,
                        generation_claim_expires_at=NOW + timedelta(minutes=5),
                        state_revision=3,
                    )
                ]
            ),
            owner_id="owner-1",
            draft_id="draft-1",
            expected_revision=2,
            claim_id=CLAIM,
            generation_log="[]",
        )
    with pytest.raises(RepositoryValidationError, match="generation_log"):
        repository.replace_generation_log_for_claim(
            ScriptedTransaction(),
            owner_id="owner-1",
            draft_id="draft-1",
            expected_revision=2,
            claim_id=CLAIM,
            generation_log="x" * 1_048_577,
        )


def test_generation_claim_renewal_uses_db_time_and_never_resurrects_or_revisions() -> None:
    repository = DraftAgentRepository()
    renewed_row = draft_row(
        status="generating",
        generation_claim_id=CLAIM,
        generation_claim_expires_at=NOW + timedelta(minutes=5),
        state_revision=2,
        updated_at=2,
    )
    transaction = ScriptedTransaction(one=[renewed_row])

    renewed = repository.renew_generation_claim(
        transaction,
        owner_id="owner-1",
        draft_id="draft-1",
        expected_revision=2,
        claim_id=CLAIM,
        lease_seconds=240,
    )

    assert renewed.state_revision == 2
    sql = transaction.fetch_sql()
    assert "clock_timestamp() + (%s * interval '1 second')" in sql
    assert "generation_claim_expires_at > clock_timestamp()" in sql
    assert "state_revision = state_revision + 1" not in sql
    assert transaction.calls[0][2] == (240, "draft-1", "owner-1", 2, CLAIM)

    for row in (None,):
        with pytest.raises(RepositoryConflictError, match="renewal fence"):
            repository.renew_generation_claim(
                ScriptedTransaction(one=[row]),
                owner_id="owner-1",
                draft_id="draft-1",
                expected_revision=2,
                claim_id=CLAIM,
            )
    with pytest.raises(RepositoryDataError, match="changed the lifecycle revision"):
        repository.renew_generation_claim(
            ScriptedTransaction(one=[{**renewed_row, "state_revision": 3}]),
            owner_id="owner-1",
            draft_id="draft-1",
            expected_revision=2,
            claim_id=CLAIM,
        )
    with pytest.raises(RepositoryValidationError, match="1800"):
        repository.renew_generation_claim(
            ScriptedTransaction(),
            owner_id="owner-1",
            draft_id="draft-1",
            expected_revision=2,
            claim_id=CLAIM,
            lease_seconds=1801,
        )


def test_exact_live_generation_claim_recovers_only_the_post_claim_revision() -> None:
    repository = DraftAgentRepository()
    claimed_row = draft_row(
        status="generating",
        generation_claim_id=CLAIM,
        generation_claim_expires_at=NOW + timedelta(minutes=5),
        state_revision=2,
    )
    transaction = ScriptedTransaction(one=[claimed_row])

    recovered = repository.get_exact_live_generation_claim(
        transaction,
        owner_id="owner-1",
        draft_id="draft-1",
        expected_preclaim_revision=1,
        claim_id=CLAIM,
    )

    assert recovered is not None
    assert recovered.state_revision == 2
    statement = transaction.fetch_sql()
    assert "state_revision = %s + 1" in statement
    assert "generation_claim_expires_at > clock_timestamp()" in statement
    assert "status = 'generating' AND published_revision_id IS NULL" in statement
    assert transaction.calls[0][2] == ("draft-1", "owner-1", 1, CLAIM)

    assert (
        repository.get_exact_live_generation_claim(
            ScriptedTransaction(one=[None]),
            owner_id="owner-1",
            draft_id="draft-1",
            expected_preclaim_revision=1,
            claim_id=CLAIM,
        )
        is None
    )
    with pytest.raises(RepositoryValidationError, match="expected_preclaim_revision"):
        repository.get_exact_live_generation_claim(
            ScriptedTransaction(),
            owner_id="owner-1",
            draft_id="draft-1",
            expected_preclaim_revision=-1,
            claim_id=CLAIM,
        )


def test_expired_generation_claim_reclaim_reselects_and_fences_prior_revision() -> None:
    repository = DraftAgentRepository()
    reclaimed_row = draft_row(
        status="generating",
        generation_claim_id=CLAIM,
        generation_claim_expires_at=NOW + timedelta(minutes=5),
        state_revision=3,
        updated_at=3,
    )
    transaction = ScriptedTransaction(one=[reclaimed_row])

    reclaimed = repository.reclaim_expired_generation_claim(
        transaction,
        owner_id="owner-1",
        draft_id="draft-1",
        expected_revision=2,
        claim_id=CLAIM,
        lease_seconds=240,
    )

    assert reclaimed.state_revision == 3
    assert reclaimed.generation_claim_id == CLAIM
    statement = transaction.fetch_sql()
    assert "generation_claim_expires_at <= clock_timestamp()" in statement
    assert "status = 'generating' AND published_revision_id IS NULL" in statement
    assert "state_revision = state_revision + 1" in statement
    assert transaction.calls[0][2] == (240, "draft-1", "owner-1", 2, CLAIM)

    with pytest.raises(RepositoryConflictError, match="reclaim fence"):
        repository.reclaim_expired_generation_claim(
            ScriptedTransaction(one=[None]),
            owner_id="owner-1",
            draft_id="draft-1",
            expected_revision=2,
            claim_id=CLAIM,
        )
    with pytest.raises(RepositoryDataError, match="returned invalid state"):
        repository.reclaim_expired_generation_claim(
            ScriptedTransaction(one=[{**reclaimed_row, "state_revision": 2}]),
            owner_id="owner-1",
            draft_id="draft-1",
            expected_revision=2,
            claim_id=CLAIM,
        )
    with pytest.raises(RepositoryValidationError, match="1800"):
        repository.reclaim_expired_generation_claim(
            ScriptedTransaction(),
            owner_id="owner-1",
            draft_id="draft-1",
            expected_revision=2,
            claim_id=CLAIM,
            lease_seconds=1801,
        )


def test_expired_generation_claim_inventory_is_db_timed_bounded_and_ordered() -> None:
    repository = DraftAgentRepository()
    older = draft_row(
        id="draft-a",
        status="generating",
        generation_claim_id=CLAIM,
        generation_claim_expires_at=NOW - timedelta(minutes=2),
        state_revision=2,
    )
    newer = draft_row(
        id="draft-b",
        status="generating",
        generation_claim_id=str(uuid.uuid4()),
        generation_claim_expires_at=NOW - timedelta(minutes=1),
        state_revision=2,
    )
    transaction = ScriptedTransaction(all_rows=[(older, newer)])

    inventory = repository.list_expired_generation_claims_for_administration(
        transaction,
        limit=2,
    )

    assert tuple(record.draft_id for record in inventory) == ("draft-a", "draft-b")
    statement = transaction.fetch_sql()
    assert "status = 'generating'" in statement
    assert "generation_claim_id IS NOT NULL" in statement
    assert "generation_claim_expires_at <= clock_timestamp()" in statement
    assert "published_revision_id IS NULL" in statement
    assert "ORDER BY generation_claim_expires_at ASC, id ASC" in statement
    assert transaction.calls[0][2] == (2,)

    cursor_transaction = ScriptedTransaction(all_rows=[(newer,)])
    page = repository.list_expired_generation_claims_for_administration(
        cursor_transaction,
        limit=2,
        after_generation_claim_expires_at=older["generation_claim_expires_at"],
        after_draft_id="draft-a",
    )
    assert tuple(record.draft_id for record in page) == ("draft-b",)
    assert "(generation_claim_expires_at, id) > (%s, %s)" in cursor_transaction.fetch_sql()
    assert cursor_transaction.calls[0][2] == (
        older["generation_claim_expires_at"],
        "draft-a",
        2,
    )

    for invalid_limit in (0, 1001, True):
        with pytest.raises(RepositoryValidationError, match="limit"):
            repository.list_expired_generation_claims_for_administration(
                ScriptedTransaction(),
                limit=invalid_limit,
            )
    for invalid_cursor in (
        {"after_generation_claim_expires_at": NOW},
        {"after_draft_id": "draft-a"},
        {
            "after_generation_claim_expires_at": datetime(2026, 8, 14),
            "after_draft_id": "draft-a",
        },
        {
            "after_generation_claim_expires_at": NOW,
            "after_draft_id": "",
        },
    ):
        with pytest.raises(
            RepositoryValidationError,
            match=r"cursor|timezone|after_draft_id",
        ):
            repository.list_expired_generation_claims_for_administration(
                ScriptedTransaction(),
                **invalid_cursor,
            )


def test_attachment_provenance_is_owner_revision_fenced_and_repeat_safe() -> None:
    repository = DraftAgentRepository()
    provenance = {
        "origin": "auto_attachment",
        "source_chat_id": "chat-1",
        "gap_fingerprint": "gap-1",
        "source_attachment_id": "attachment-1",
        "state_revision": 1,
        "updated_at": 2,
    }
    transaction = ScriptedTransaction(one=[draft_row(**provenance)])
    bound = repository.bind_attachment_provenance(
        transaction,
        owner_id="owner-1",
        draft_id="draft-1",
        expected_revision=0,
        source_chat_id="chat-1",
        gap_fingerprint="gap-1",
        source_attachment_id="attachment-1",
        updated_at=2,
    )
    assert bound.source_attachment_id == "attachment-1"
    assert "user_id = %s AND state_revision = %s" in transaction.fetch_sql()
    assert "source_attachment_id IS NULL" in transaction.fetch_sql()

    replay = repository.bind_attachment_provenance(
        ScriptedTransaction(one=[None, draft_row(**(provenance | {"state_revision": 4}))]),
        owner_id="owner-1",
        draft_id="draft-1",
        expected_revision=0,
        source_chat_id="chat-1",
        gap_fingerprint="gap-1",
        source_attachment_id="attachment-1",
        updated_at=2,
    )
    assert replay.state_revision == 4

    with pytest.raises(RepositoryConflictError, match="provenance"):
        repository.bind_attachment_provenance(
            ScriptedTransaction(
                one=[
                    None,
                    draft_row(**(provenance | {"source_attachment_id": "other"})),
                ]
            ),
            owner_id="owner-1",
            draft_id="draft-1",
            expected_revision=0,
            source_chat_id="chat-1",
            gap_fingerprint="gap-1",
            source_attachment_id="attachment-1",
            updated_at=2,
        )


def test_create_draft_replay_fences_source_attachment_identity() -> None:
    repository = DraftAgentRepository()
    with pytest.raises(RepositoryConflictError, match="immutable"):
        repository.create_draft(
            ScriptedTransaction(one=[None, draft_row(source_attachment_id="attachment-other")]),
            draft_id="draft-1",
            owner_id="owner-1",
            agent_name="Research Agent",
            agent_slug="research-agent",
            description="Draft",
            observed_at=1,
            source_attachment_id="attachment-1",
            draft_uuid=DRAFT_UUID,
            target_agent_id="agent-1",
        )

    with pytest.raises(RepositoryConflictError, match="immutable"):
        repository.create_draft(
            ScriptedTransaction(
                one=[
                    None,
                    draft_row(
                        plan_json='{"tasks":["different"]}',
                        constitution_version="0.2.0",
                    ),
                ]
            ),
            draft_id="draft-1",
            owner_id="owner-1",
            agent_name="Research Agent",
            agent_slug="research-agent",
            description="Draft",
            observed_at=1,
            plan_json='{"tasks":["original"]}',
            constitution_version="0.1.0",
            draft_uuid=DRAFT_UUID,
            target_agent_id="agent-1",
        )


def test_stale_draft_revision_is_classified_without_cross_owner_disclosure() -> None:
    repository = DraftAgentRepository()
    with pytest.raises(RepositoryConflictError, match="revision"):
        repository.compare_and_set_draft(
            ScriptedTransaction(one=[None, {"state_revision": 4}]),
            owner_id="owner-1",
            draft_id="draft-1",
            expected_revision=0,
            updates={"status": "analyzing"},
            updated_at=2,
        )


def test_transition_and_publication_replays_preserve_semantics() -> None:
    repository = DraftAgentRepository()
    transition = repository.record_transition(
        ScriptedTransaction(one=[transition_row()]),
        transition_id=TRANSITION,
        draft_uuid=DRAFT_UUID,
        owner_id="owner-1",
        operation_execution_generation=1,
        transition_kind="advance_phase",
        expected_revision=0,
        result_revision=1,
        outcome="applied",
    )
    assert transition.result_revision == 1

    publication = repository._create_publication(
        ScriptedTransaction(one=[publication_row()]),
        _capability=_GENERATED_PUBLICATION_MUTATION_CAPABILITY,
        publication_id=PUBLICATION,
        draft_uuid=DRAFT_UUID,
        owner_id="owner-1",
        source_state_revision=1,
        generation_claim_id=CLAIM,
        target_agent_id="agent-1",
        target_revision_id=TARGET_REVISION,
        operation_execution_generation=1,
        staging_relative_path=STAGING_PATH,
        revision_relative_path=REVISION_PATH,
    )
    assert publication.state == "claimed"

    completed = repository._transition_publication(
        ScriptedTransaction(
            one=[
                publication_row(
                    state="published",
                    state_revision=1,
                    artifact_digest=DIGEST,
                    manifest_digest=DIGEST,
                    published_at=NOW,
                )
            ]
        ),
        _capability=_GENERATED_PUBLICATION_MUTATION_CAPABILITY,
        owner_id="owner-1",
        publication_id=PUBLICATION,
        expected_revision=0,
        expected_state="claimed",
        updates={
            "state": "published",
            "artifact_digest": DIGEST,
            "manifest_digest": DIGEST,
            "published_at": NOW,
        },
    )
    assert completed.published_at == NOW


@pytest.mark.parametrize(
    ("staging_path", "revision_path"),
    (
        ("../escape", REVISION_PATH),
        (f".staging/{DRAFT_UUID}/1/{PUBLICATION}", REVISION_PATH),
        (f"staging//{DRAFT_UUID}/1/{PUBLICATION}", REVISION_PATH),
        (f"staging/{DRAFT_UUID}/./1/{PUBLICATION}", REVISION_PATH),
        (rf"C:\staging\{DRAFT_UUID}\1\{PUBLICATION}", REVISION_PATH),
        (f"staging/{DRAFT_UUID}/2/{PUBLICATION}", REVISION_PATH),
        (STAGING_PATH, f"revisions/other/{TARGET_REVISION}"),
        (STAGING_PATH, f"revisions/agent-1//{TARGET_REVISION}"),
        (STAGING_PATH, rf"revisions\agent-1\{TARGET_REVISION}"),
    ),
)
def test_publication_paths_and_transition_kinds_are_bounded(
    staging_path: str,
    revision_path: str,
) -> None:
    repository = DraftAgentRepository()
    with pytest.raises(RepositoryValidationError):
        repository._create_publication(
            ScriptedTransaction(),
            _capability=_GENERATED_PUBLICATION_MUTATION_CAPABILITY,
            publication_id=PUBLICATION,
            draft_uuid=DRAFT_UUID,
            owner_id="owner-1",
            source_state_revision=1,
            generation_claim_id=CLAIM,
            target_agent_id="agent-1",
            target_revision_id=TARGET_REVISION,
            staging_relative_path=staging_path,
            revision_relative_path=revision_path,
        )
    with pytest.raises(RepositoryValidationError):
        repository.record_transition(
            ScriptedTransaction(),
            transition_id=TRANSITION,
            draft_uuid=DRAFT_UUID,
            owner_id="owner-1",
            operation_execution_generation=1,
            transition_kind="BAD-KIND",
            expected_revision=0,
            result_revision=0,
            outcome="failed",
        )


def test_raw_publication_mutators_are_not_public_and_require_private_capability() -> None:
    repository = DraftAgentRepository()
    assert not hasattr(repository, "create_publication")
    assert not hasattr(repository, "transition_publication")
    with pytest.raises(RepositoryValidationError, match="capability"):
        repository._create_publication(
            ScriptedTransaction(),
            _capability=object(),
            publication_id=PUBLICATION,
            draft_uuid=DRAFT_UUID,
            owner_id="owner-1",
            source_state_revision=1,
            generation_claim_id=CLAIM,
            target_agent_id="agent-1",
            target_revision_id=TARGET_REVISION,
            staging_relative_path=STAGING_PATH,
            revision_relative_path=REVISION_PATH,
        )
    with pytest.raises(RepositoryValidationError, match="capability"):
        repository._transition_publication(
            ScriptedTransaction(),
            _capability=object(),
            owner_id="owner-1",
            publication_id=PUBLICATION,
            expected_revision=0,
            expected_state="claimed",
            updates={"state": "staged"},
        )


def test_draft_slug_gap_and_bounded_lists_keep_owner_predicates() -> None:
    repository = DraftAgentRepository()
    assert (
        repository.get_draft_by_slug(
            ScriptedTransaction(one=[draft_row()]),
            owner_id="owner-1",
            agent_slug="research-agent",
        ).draft_id
        == "draft-1"
    )  # type: ignore[union-attr]
    assert (
        repository.get_draft_by_slug(
            ScriptedTransaction(one=[None]),
            owner_id="other",
            agent_slug="research-agent",
        )
        is None
    )
    gap = draft_row(source_chat_id="chat-1", gap_fingerprint="capability-1")
    assert (
        repository.find_gap_draft(
            ScriptedTransaction(one=[gap]),
            owner_id="owner-1",
            source_chat_id="chat-1",
            gap_fingerprint="capability-1",
        ).gap_fingerprint
        == "capability-1"
    )  # type: ignore[union-attr]
    assert (
        repository.list_drafts(
            ScriptedTransaction(all_rows=[(draft_row(),)]),
            owner_id="owner-1",
            origin="manual",
            include_terminal=False,
            limit=1,
        )[0].owner_id
        == "owner-1"
    )
    assert (
        repository.list_pending_review_for_administration(
            ScriptedTransaction(
                all_rows=[(draft_row(status="pending_review", reviewed_by="admin"),)]
            ),
            limit=1,
        )[0].status
        == "pending_review"
    )
    admin_slug = ScriptedTransaction(one=[draft_row()])
    assert (
        repository.get_draft_by_slug_for_administration(
            admin_slug,
            agent_slug="research-agent",
        ).draft_id
        == "draft-1"
    )  # type: ignore[union-attr]
    assert "created_at DESC NULLS LAST, id ASC" in admin_slug.fetch_sql()
    admin_list = ScriptedTransaction(all_rows=[(draft_row(),)])
    assert repository.list_drafts_for_administration(admin_list, limit=1)[0].owner_id == "owner-1"
    assert admin_list.calls[0][2] == (1,)


def test_finish_generation_and_delete_are_exact_owner_claim_operations() -> None:
    repository = DraftAgentRepository()
    finished = repository.finish_generation(
        ScriptedTransaction(one=[draft_row(status="generated", state_revision=2, updated_at=3)]),
        owner_id="owner-1",
        draft_id="draft-1",
        expected_revision=1,
        claim_id=CLAIM,
        status="generated",
        security_report="{}",
        validation_report="{}",
    )
    assert finished.status == "generated"
    with pytest.raises(RepositoryConflictError):
        repository.finish_generation(
            ScriptedTransaction(one=[None]),
            owner_id="owner-1",
            draft_id="draft-1",
            expected_revision=1,
            claim_id=CLAIM,
            status="error",
        )
    with pytest.raises(RepositoryValidationError):
        repository.finish_generation(
            ScriptedTransaction(),
            owner_id="owner-1",
            draft_id="draft-1",
            expected_revision=1,
            claim_id=CLAIM,
            status="live",
        )
    assert repository.delete_draft(
        ScriptedTransaction(one=[{"id": "draft-1"}]),
        owner_id="owner-1",
        draft_id="draft-1",
    )
    assert not repository.delete_draft(
        ScriptedTransaction(one=[None]), owner_id="other", draft_id="draft-1"
    )


def test_transition_and_publication_getters_are_owner_scoped() -> None:
    repository = DraftAgentRepository()
    assert (
        repository.get_transition(
            ScriptedTransaction(one=[transition_row()]),
            owner_id="owner-1",
            transition_id=TRANSITION,
        ).transition_id
        == TRANSITION
    )  # type: ignore[union-attr]
    assert (
        repository.get_transition(
            ScriptedTransaction(one=[None]),
            owner_id="other",
            transition_id=TRANSITION,
        )
        is None
    )
    assert (
        repository.get_publication(
            ScriptedTransaction(one=[publication_row()]),
            owner_id="owner-1",
            publication_id=PUBLICATION,
        ).publication_id
        == PUBLICATION
    )  # type: ignore[union-attr]
    assert (
        repository.get_publication(
            ScriptedTransaction(one=[None]),
            owner_id="other",
            publication_id=PUBLICATION,
        )
        is None
    )
    with pytest.raises(RepositoryConflictError):
        repository._transition_publication(
            ScriptedTransaction(one=[None]),
            _capability=_GENERATED_PUBLICATION_MUTATION_CAPABILITY,
            owner_id="owner-1",
            publication_id=PUBLICATION,
            expected_revision=0,
            expected_state="claimed",
            updates={"state": "staged"},
        )


def test_publication_recovery_getters_and_inventory_preserve_owner_and_bounds() -> None:
    repository = DraftAgentRepository()

    draft_transaction = ScriptedTransaction(one=[draft_row()])
    assert (
        repository.get_draft_by_uuid(
            draft_transaction,
            owner_id="owner-1",
            draft_uuid=DRAFT_UUID,
            for_update=True,
        ).draft_uuid
        == DRAFT_UUID
    )  # type: ignore[union-attr]
    assert draft_transaction.calls[0][2] == (DRAFT_UUID, "owner-1")
    assert "FOR UPDATE" in draft_transaction.fetch_sql()

    source_transaction = ScriptedTransaction(one=[publication_row()])
    assert (
        repository.get_publication_by_source(
            source_transaction,
            owner_id="owner-1",
            draft_uuid=DRAFT_UUID,
            source_state_revision=1,
        ).publication_id
        == PUBLICATION
    )  # type: ignore[union-attr]
    assert source_transaction.calls[0][2] == ("owner-1", DRAFT_UUID, 1)

    target_transaction = ScriptedTransaction(one=[publication_row()])
    assert (
        repository.get_publication_by_target_revision(
            target_transaction,
            owner_id="owner-1",
            target_agent_id="agent-1",
            target_revision_id=TARGET_REVISION,
            for_update=True,
        ).publication_id
        == PUBLICATION
    )  # type: ignore[union-attr]
    assert target_transaction.calls[0][2] == (
        "owner-1",
        "agent-1",
        TARGET_REVISION,
    )
    assert "FOR UPDATE" in target_transaction.fetch_sql()

    inventory_transaction = ScriptedTransaction(all_rows=[(publication_row(),)])
    inventory = repository.list_reconcilable_publications_for_administration(
        inventory_transaction,
        limit=10,
    )
    assert inventory[0].publication_id == PUBLICATION
    assert "state IN ('claimed', 'staged', 'validated')" in inventory_transaction.fetch_sql()
    assert "ORDER BY created_at, publication_id" in inventory_transaction.fetch_sql()
    assert inventory_transaction.calls[0][2] == (10,)

    cursor_transaction = ScriptedTransaction(all_rows=[(publication_row(),)])
    cursor_inventory = repository.list_reconcilable_publications_for_administration(
        cursor_transaction,
        limit=10,
        after_created_at=NOW,
        after_publication_id=PUBLICATION,
    )
    assert cursor_inventory[0].publication_id == PUBLICATION
    assert "(created_at, publication_id) > (%s, %s)" in cursor_transaction.fetch_sql()
    assert cursor_transaction.calls[0][2] == (NOW, PUBLICATION, 10)

    for invalid_cursor in (
        {"after_created_at": NOW},
        {"after_publication_id": PUBLICATION},
        {
            "after_created_at": datetime(2026, 8, 14),
            "after_publication_id": PUBLICATION,
        },
        {"after_created_at": NOW, "after_publication_id": "not-a-uuid"},
        {"after_created_at": NOW, "after_publication_id": uuid.UUID(PUBLICATION)},
    ):
        with pytest.raises(
            RepositoryValidationError,
            match=r"cursor|timezone|UUID|after_publication_id",
        ):
            repository.list_reconcilable_publications_for_administration(
                ScriptedTransaction(),
                **invalid_cursor,
            )


def test_transition_and_publication_replay_conflicts_are_detected() -> None:
    repository = DraftAgentRepository()
    with pytest.raises(RepositoryConflictError, match="replay"):
        repository.record_transition(
            ScriptedTransaction(one=[None, transition_row(transition_kind="save_artifact")]),
            transition_id=TRANSITION,
            draft_uuid=DRAFT_UUID,
            owner_id="owner-1",
            operation_execution_generation=1,
            transition_kind="advance_phase",
            expected_revision=0,
            result_revision=1,
            outcome="applied",
        )
    with pytest.raises(RepositoryConflictError, match="replay"):
        repository._create_publication(
            ScriptedTransaction(one=[None, publication_row(source_state_revision=2)]),
            _capability=_GENERATED_PUBLICATION_MUTATION_CAPABILITY,
            publication_id=PUBLICATION,
            draft_uuid=DRAFT_UUID,
            owner_id="owner-1",
            source_state_revision=1,
            generation_claim_id=CLAIM,
            target_agent_id="agent-1",
            target_revision_id=TARGET_REVISION,
            staging_relative_path=STAGING_PATH,
            revision_relative_path=REVISION_PATH,
        )


def test_create_replay_owner_conflict_and_draft_update_validation() -> None:
    repository = DraftAgentRepository()
    with pytest.raises(RepositoryConflictError, match="another owner"):
        repository.create_draft(
            ScriptedTransaction(one=[None, None]),
            draft_id="draft-1",
            owner_id="owner-1",
            agent_name="Research Agent",
            agent_slug="research-agent",
            description="Draft",
            observed_at=1,
            draft_uuid=DRAFT_UUID,
            target_agent_id="agent-1",
        )
    with pytest.raises(RepositoryValidationError):
        repository.compare_and_set_draft(
            ScriptedTransaction(),
            owner_id="owner-1",
            draft_id="draft-1",
            expected_revision=0,
            updates={"user_id": "other"},
            updated_at=2,
        )
    with pytest.raises(RepositoryValidationError):
        repository._transition_publication(
            ScriptedTransaction(),
            _capability=_GENERATED_PUBLICATION_MUTATION_CAPABILITY,
            owner_id="owner-1",
            publication_id=PUBLICATION,
            expected_revision=0,
            expected_state="claimed",
            updates={"owner_user_id": "other"},
        )


class _TruncatingFence:
    def __int__(self) -> int:
        return int(1.9)


@pytest.mark.parametrize(
    "fence",
    [1.9, float("inf"), float("nan"), Decimal("1.9"), True, _TruncatingFence(), "1.9", -1],
)
def test_revision_fences_reject_malformed_values_before_sql(fence: object) -> None:
    repository = DraftAgentRepository()

    with pytest.raises(RepositoryValidationError, match="expected_revision"):
        repository.compare_and_set_draft(
            ScriptedTransaction(),
            owner_id="owner-1",
            draft_id="draft-1",
            expected_revision=fence,
            updates={"status": "analyzing"},
            updated_at=2,
        )
    with pytest.raises(RepositoryValidationError, match="expected_revision"):
        repository.bind_attachment_provenance(
            ScriptedTransaction(),
            owner_id="owner-1",
            draft_id="draft-1",
            expected_revision=fence,
            source_chat_id="chat-1",
            gap_fingerprint="gap-1",
            source_attachment_id="attachment-1",
            updated_at=2,
        )


def test_revision_fences_pass_exact_integer_values_to_the_owner_scoped_statement() -> None:
    repository = DraftAgentRepository()

    for fence in (2, 2.0, Decimal("2.00"), "2"):
        transaction = ScriptedTransaction(one=[draft_row(state_revision=3)])
        updated = repository.compare_and_set_draft(
            transaction,
            owner_id="owner-1",
            draft_id="draft-1",
            expected_revision=fence,
            updates={"status": "analyzing"},
            updated_at=2,
        )
        assert updated.state_revision == 3
        assert transaction.calls[0][2][-2:] == ("owner-1", 2)
        assert "user_id = %s AND state_revision = %s" in transaction.calls[0][1]


def test_draft_cas_owner_mismatch_is_not_found_without_cross_owner_disclosure() -> None:
    repository = DraftAgentRepository()

    with pytest.raises(RepositoryNotFoundError, match="owner-scoped draft"):
        repository.compare_and_set_draft(
            ScriptedTransaction(one=[None, None]),
            owner_id="other-owner",
            draft_id="draft-1",
            expected_revision=0,
            updates={"status": "analyzing"},
            updated_at=2,
        )
    with pytest.raises(RepositoryNotFoundError, match="owner-scoped draft"):
        repository.bind_attachment_provenance(
            ScriptedTransaction(one=[None, None]),
            owner_id="other-owner",
            draft_id="draft-1",
            expected_revision=0,
            source_chat_id="chat-1",
            gap_fingerprint="gap-1",
            source_attachment_id="attachment-1",
            updated_at=2,
        )
