"""Live-PostgreSQL tests for astralplane.repositories.generated_agent_publications:
crash/commit/rollback durability, reconcilable-inventory fairness, and
begin/replay/claim-renewal fencing.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from astralplane.database.transaction import PlaneDatabase
from astralplane.immutable_bundle_store import FinalizedBundle, canonical_bundle_digest
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.agents import AgentRepository
from astralplane.repositories.drafts import (
    DraftAgentRecord,
    DraftAgentRepository,
    DraftPublicationRecord,
)
from astralplane.repositories.generated_agent_publications import (
    GENERATED_AGENT_BUNDLE_CONTRACT,
    GeneratedAgentPublicationOperationBinding,
    GeneratedAgentPublicationRepository,
    GeneratedAgentPublicationResultMetadata,
    generated_agent_publication_operation_binding,
    generated_agent_publication_paths,
    generated_agent_publication_recovery_operation_binding,
)
from astralplane.repositories.work_admission import (
    AcceptedAdmission,
    AdmissionClass,
    ExecutionFence,
    OperationOwner,
    OperationRequest,
    OperationState,
    OwnerScope,
    WorkAdmissionRepository,
)
from tests.fixtures.migrated_template import MigratedDatabase
from tests.integration.test_lifecycle_acceptance import concurrent_database

OWNER = "owner-generated-publication-integration"
AGENT_ID = "generated-publication-agent"
LOCK_DIGEST = "b" * 64
BUNDLE_FILES = {
    "agent_main.py": "main\n",
    "astralprims_ui.py": "ui\n",
    "protected_executor.py": "executor\n",
    "mcp_tools.py": "tools\n",
}
ARTIFACT_DIGEST = canonical_bundle_digest(
    BUNDLE_FILES,
    GENERATED_AGENT_BUNDLE_CONTRACT,
)
RESULT_METADATA = GeneratedAgentPublicationResultMetadata(
    error_message="Validation found one non-blocking issue.",
    security_report='{"findings":[]}',
    validation_report='{"passed":true}',
    required_credentials='["api_key"]',
)


@dataclass(slots=True)
class _PublicationFixture:
    database_url: str
    connection: Any
    schema: str
    database: PlaneDatabase
    agents: AgentRepository
    drafts: DraftAgentRepository
    work: WorkAdmissionRepository
    publications: GeneratedAgentPublicationRepository

    @property
    def dsn(self):
        return self.database_url


@dataclass(frozen=True, slots=True)
class _PreparedPublication:
    draft_id: str
    draft_uuid: str
    claim_id: str
    source_state_revision: int
    publication_id: str
    revision_id: str
    promotion_token: str
    bundle: FinalizedBundle
    operation_binding: GeneratedAgentPublicationOperationBinding


@dataclass(frozen=True, slots=True)
class _SeededPublication:
    prepared: _PreparedPublication
    publication: DraftPublicationRecord
    attempt: ExecutionFence


@pytest.fixture
def publication_postgres(migrated_clone: MigratedDatabase) -> _PublicationFixture:
    agents = AgentRepository()
    drafts = DraftAgentRepository()
    work = WorkAdmissionRepository()
    with migrated_clone.database.transaction() as transaction:
        work.bind_configs(work.load_existing_configs(transaction))
    publications = GeneratedAgentPublicationRepository(
        agents=agents,
        drafts=drafts,
        work_admission=work,
    )
    return _PublicationFixture(
        database_url=migrated_clone.dsn,
        connection=migrated_clone.connection,
        schema=migrated_clone.schema,
        database=migrated_clone.database,
        agents=agents,
        drafts=drafts,
        work=work,
        publications=publications,
    )


def _bundle(revision_id: str, *, content_suffix: str = "") -> FinalizedBundle:
    files = dict(BUNDLE_FILES)
    files["agent_main.py"] = f"{files['agent_main.py']}{content_suffix}"
    artifact_digest = canonical_bundle_digest(files, GENERATED_AGENT_BUNDLE_CONTRACT)
    manifest = {
        "agent_id": AGENT_ID,
        "agent_name": "Generated publication integration",
        "bundle_sha256": artifact_digest,
        "constitution_version": "0.1.0",
        "description": "Synthetic non-PHI fixture",
        "digest_algorithm": "sha256",
        "files": [
            {
                "name": name,
                "sha256": hashlib.sha256(files[name].encode("utf-8")).hexdigest(),
                "size_bytes": len(files[name].encode("utf-8")),
            }
            for name in GENERATED_AGENT_BUNDLE_CONTRACT.file_names
        ],
        "manifest_version": 2,
        "required_runtime_lock_sha256": LOCK_DIGEST,
        "revision_id": revision_id,
        "runtime_contract_version": 3,
    }
    manifest_json = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )
    return FinalizedBundle(
        contract=GENERATED_AGENT_BUNDLE_CONTRACT,
        files=files,
        bundle_sha256=artifact_digest,
        manifest=manifest,
        manifest_json=manifest_json,
    )


def _attempt(
    fixture: _PublicationFixture,
    binding: GeneratedAgentPublicationOperationBinding,
) -> ExecutionFence:
    now = datetime.now(UTC)
    request = OperationRequest(
        operation_kind=binding.operation_kind,
        admission_class=AdmissionClass.INTERACTIVE,
        owner=OperationOwner(OwnerScope.USER, OWNER, None),
        submission_id=uuid.uuid4(),
        idempotency_namespace=binding.idempotency_namespace,
        idempotency_key=binding.idempotency_key,
        normalized_input_digest=binding.normalized_input_digest,
        chat_id=None,
        parent_operation_id=binding.parent_operation_id,
        connection_generation=None,
        request_generation=uuid.uuid4(),
    )
    with fixture.database.transaction() as transaction:
        accepted = fixture.work.submit(
            transaction,
            request,
            now=now,
            retention=timedelta(days=1),
            slot_lease=timedelta(minutes=5),
        )
    assert isinstance(accepted, AcceptedAdmission)
    with fixture.database.transaction() as transaction:
        claim = fixture.work.claim_operation(
            transaction,
            AdmissionClass.INTERACTIVE,
            accepted.operation_id,
            now=now,
            retention=timedelta(days=1),
            slot_lease=timedelta(minutes=5),
        )
    assert claim is not None
    return claim.fence


def _begin_kwargs(
    prepared: _PreparedPublication,
    attempt: ExecutionFence,
) -> dict[str, object]:
    paths = generated_agent_publication_paths(
        draft_uuid=prepared.draft_uuid,
        source_state_revision=prepared.source_state_revision,
        publication_id=prepared.publication_id,
        target_agent_id=AGENT_ID,
        target_revision_id=prepared.revision_id,
    )
    return {
        "owner_id": OWNER,
        "publication_id": prepared.publication_id,
        "draft_uuid": prepared.draft_uuid,
        "source_state_revision": prepared.source_state_revision,
        "generation_claim_id": prepared.claim_id,
        "target_agent_id": AGENT_ID,
        "target_revision_id": prepared.revision_id,
        "staging_relative_path": paths.staging_relative_path,
        "revision_relative_path": paths.revision_relative_path,
        "bundle": prepared.bundle,
        "runtime_contract_version": 3,
        "release_lock_digest": LOCK_DIGEST,
        "promotion_token": prepared.promotion_token,
        "attempt": attempt,
    }


def _prepare_source(
    fixture: _PublicationFixture,
    *,
    create_agent: bool = True,
    content_suffix: str = "",
) -> _PreparedPublication:
    draft_uuid = str(uuid.uuid4())
    draft_id = f"draft-{uuid.uuid4()}"
    revision_id = str(uuid.uuid4())
    publication_id = str(uuid.uuid4())
    claim_id = str(uuid.uuid4())
    promotion_token = str(uuid.uuid4())
    bundle = _bundle(revision_id, content_suffix=content_suffix)
    with fixture.database.transaction() as transaction:
        fixture.drafts.create_draft(
            transaction,
            draft_id=draft_id,
            owner_id=OWNER,
            agent_name="Generated publication integration",
            agent_slug=f"generated-publication-{uuid.uuid4().hex}",
            description="synthetic non-PHI fixture",
            observed_at=1,
            draft_uuid=draft_uuid,
            target_agent_id=AGENT_ID,
        )
        if create_agent:
            fixture.agents.create_agent(
                transaction,
                agent_id=AGENT_ID,
                owner_id=OWNER,
                display_name="Generated publication integration",
                observed_at=1,
                draft_id=draft_id,
            )
    with fixture.database.transaction() as transaction:
        claimed = fixture.drafts.claim_generation(
            transaction,
            owner_id=OWNER,
            draft_id=draft_id,
            expected_revision=0,
            claim_id=claim_id,
            lease_seconds=1800,
        )
    binding = generated_agent_publication_operation_binding(
        owner_id=OWNER,
        publication_id=publication_id,
        draft_uuid=draft_uuid,
        source_state_revision=claimed.state_revision,
        generation_claim_id=claim_id,
        target_agent_id=AGENT_ID,
        target_revision_id=revision_id,
        bundle=bundle,
        runtime_contract_version=3,
        release_lock_digest=LOCK_DIGEST,
        promotion_token=promotion_token,
    )
    return _PreparedPublication(
        draft_id=draft_id,
        draft_uuid=draft_uuid,
        claim_id=claim_id,
        source_state_revision=claimed.state_revision,
        publication_id=publication_id,
        revision_id=revision_id,
        promotion_token=promotion_token,
        bundle=bundle,
        operation_binding=binding,
    )


def _seed(
    fixture: _PublicationFixture,
    *,
    create_agent: bool = True,
    content_suffix: str = "",
) -> _SeededPublication:
    prepared = _prepare_source(
        fixture,
        create_agent=create_agent,
        content_suffix=content_suffix,
    )
    attempt = _attempt(fixture, prepared.operation_binding)
    with fixture.database.transaction() as transaction:
        intent = fixture.publications.begin_intent(
            transaction,
            **_begin_kwargs(prepared, attempt),
        )
    assert intent.revision.state == "prepared"
    return _SeededPublication(prepared, intent.publication, attempt)


def _terminalize(
    fixture: _PublicationFixture,
    fence: ExecutionFence,
    *,
    state: OperationState,
) -> None:
    with fixture.database.transaction() as transaction:
        fixture.work.terminalize(
            transaction,
            fence,
            state=state,
            terminal_code=None if state is OperationState.COMPLETED else "worker_restarted",
            safe_summary="Publication operation terminalized",
            retry_after_ms=None,
            now=datetime.now(UTC),
            retention=timedelta(days=1),
        )


def _load_draft(
    fixture: _PublicationFixture,
    prepared: _PreparedPublication,
) -> DraftAgentRecord:
    with fixture.database.transaction() as transaction:
        result = fixture.drafts.get_draft(
            transaction,
            owner_id=OWNER,
            draft_id=prepared.draft_id,
        )
    assert result is not None
    return result


def test_live_postgres_validation_evidence_survives_crash_and_commit_rollback(
    publication_postgres: _PublicationFixture,
) -> None:
    fixture = publication_postgres
    seeded = _seed(fixture)
    with fixture.database.transaction() as transaction:
        fixture.publications.assert_current_attempt(
            transaction,
            expected=seeded.publication,
            attempt=seeded.attempt,
        )
        staged = fixture.publications.mark_staged(
            transaction,
            expected=seeded.publication,
            attempt=seeded.attempt,
        )
    with fixture.database.transaction() as transaction:
        validated = fixture.publications.mark_validated(
            transaction,
            expected=staged,
            attempt=seeded.attempt,
            artifact_digest=ARTIFACT_DIGEST,
            manifest_digest=seeded.prepared.bundle.manifest_sha256,
            generation_result=RESULT_METADATA,
        )

    after_validation = _load_draft(fixture, seeded.prepared)
    assert after_validation.state_revision == validated.source_state_revision
    assert after_validation.generation_claim_id == validated.generation_claim_id
    assert after_validation.error_message == RESULT_METADATA.error_message
    assert after_validation.security_report == RESULT_METADATA.security_report
    assert after_validation.validation_report == RESULT_METADATA.validation_report
    assert after_validation.required_credentials == RESULT_METADATA.required_credentials

    with (
        pytest.raises(RuntimeError, match="rollback publication commit"),
        fixture.database.transaction() as transaction,
    ):
        fixture.publications.commit_published(
            transaction,
            expected=validated,
            attempt=seeded.attempt,
            generation_result=RESULT_METADATA,
        )
        raise RuntimeError("rollback publication commit")

    with fixture.database.transaction() as transaction:
        after_rollback = fixture.publications.get_by_target_revision(
            transaction,
            owner_id=OWNER,
            target_agent_id=AGENT_ID,
            target_revision_id=seeded.prepared.revision_id,
        )
    draft_after_rollback = _load_draft(fixture, seeded.prepared)
    assert after_rollback == validated
    assert draft_after_rollback.generation_claim_id == validated.generation_claim_id
    assert draft_after_rollback.published_revision_id is None
    assert draft_after_rollback.validation_report == RESULT_METADATA.validation_report

    _terminalize(fixture, seeded.attempt, state=OperationState.FAILED)
    with fixture.database.transaction() as transaction:
        prepared_revision = fixture.agents.get_revision(
            transaction,
            owner_id=OWNER,
            agent_id=AGENT_ID,
            revision_id=seeded.prepared.revision_id,
        )
    assert prepared_revision is not None
    recovery_binding = generated_agent_publication_recovery_operation_binding(
        validated,
        prepared_revision,
    )
    recovery_attempt = _attempt(fixture, recovery_binding)
    with fixture.database.transaction() as transaction:
        rebound = fixture.publications.rebind_recovery_attempt(
            transaction,
            expected=validated,
            new_attempt=recovery_attempt,
        )
    restarted_publications = GeneratedAgentPublicationRepository()
    with fixture.database.transaction() as transaction:
        published = restarted_publications.commit_published(
            transaction,
            expected=rebound,
            attempt=recovery_attempt,
        )
    assert published.state == "published"

    final_draft = _load_draft(fixture, seeded.prepared)
    assert final_draft.status == "generated"
    assert final_draft.generation_claim_id is None
    assert final_draft.published_revision_id == seeded.prepared.revision_id
    assert final_draft.security_report == RESULT_METADATA.security_report
    assert final_draft.validation_report == RESULT_METADATA.validation_report
    assert final_draft.required_credentials == RESULT_METADATA.required_credentials

    with fixture.database.transaction() as transaction:
        revision = fixture.agents.get_revision(
            transaction,
            owner_id=OWNER,
            agent_id=AGENT_ID,
            revision_id=seeded.prepared.revision_id,
        )
        agent = fixture.agents.get_agent(
            transaction,
            owner_id=OWNER,
            agent_id=AGENT_ID,
        )
    assert revision is not None and revision.state == "prepared"
    assert agent is not None and agent.active_revision_id is None

    _terminalize(fixture, recovery_attempt, state=OperationState.COMPLETED)
    with fixture.database.transaction() as transaction:
        failed_revision = fixture.agents.transition_revision(
            transaction,
            owner_id=OWNER,
            agent_id=AGENT_ID,
            revision_id=seeded.prepared.revision_id,
            expected_revision=revision.state_revision,
            expected_state="prepared",
            updates={
                "state": "failed",
                "failed_at": datetime.now(UTC),
                "failure_code": "runtime_start_failed",
            },
        )
    assert failed_revision.state == "failed"
    with fixture.database.transaction() as transaction:
        assert (
            fixture.publications.commit_published(
                transaction,
                expected=rebound,
                attempt=recovery_attempt,
                generation_result=RESULT_METADATA,
            )
            == published
        )
    with (
        pytest.raises(RepositoryConflictError, match="result metadata"),
        fixture.database.transaction() as transaction,
    ):
        fixture.publications.commit_published(
            transaction,
            expected=rebound,
            attempt=recovery_attempt,
            generation_result=replace(
                RESULT_METADATA,
                validation_report='{"passed":false}',
            ),
        )

    unrelated = _attempt(
        fixture,
        replace(
            seeded.prepared.operation_binding,
            idempotency_key=f"unrelated-{uuid.uuid4()}",
            normalized_input_digest="c" * 64,
        ),
    )
    with (
        pytest.raises(RepositoryConflictError, match="operation attempt"),
        fixture.database.transaction() as transaction,
    ):
        fixture.publications.commit_published(
            transaction,
            expected=rebound,
            attempt=unrelated,
        )


def test_live_postgres_reconcilable_inventory_keyset_has_no_starvation_or_duplicates(
    publication_postgres: _PublicationFixture,
) -> None:
    fixture = publication_postgres
    seeded_rows: list[_SeededPublication] = []
    for index in range(4):
        seeded = _seed(
            fixture,
            create_agent=index == 0,
            content_suffix=f"pagination-{index}\n",
        )
        seeded_rows.append(seeded)
        _terminalize(fixture, seeded.attempt, state=OperationState.RETRYABLE)

    identical_created_at = datetime(2000, 1, 1, tzinfo=UTC)
    with fixture.database.transaction() as transaction:
        for seeded in seeded_rows:
            result = transaction.execute(
                "UPDATE draft_artifact_publication SET created_at = %s WHERE publication_id = %s",
                (identical_created_at, seeded.publication.publication_id),
            )
            assert result.rowcount == 1

    with fixture.database.transaction() as transaction:
        first_page = fixture.publications.list_reconcilable_for_administration(
            transaction,
            limit=2,
        )
    assert len(first_page) == 2
    assert all(row.created_at == identical_created_at for row in first_page)

    with fixture.database.transaction() as transaction:
        second_page = fixture.publications.list_reconcilable_for_administration(
            transaction,
            limit=2,
            after_created_at=first_page[-1].created_at,
            after_publication_id=first_page[-1].publication_id,
        )
    with fixture.database.transaction() as transaction:
        exhausted = fixture.publications.list_reconcilable_for_administration(
            transaction,
            limit=2,
            after_created_at=second_page[-1].created_at,
            after_publication_id=second_page[-1].publication_id,
        )

    observed = [row.publication_id for row in (*first_page, *second_page)]
    expected = sorted(row.publication.publication_id for row in seeded_rows)
    assert observed == expected
    assert len(observed) == len(set(observed)) == 4
    assert exhausted == ()

    with (
        pytest.raises(RepositoryValidationError, match="supplied together"),
        fixture.database.transaction() as transaction,
    ):
        fixture.publications.list_reconcilable_for_administration(
            transaction,
            after_created_at=identical_created_at,
        )


def test_live_postgres_begin_replay_paths_claim_renewal_and_revision_fences(
    publication_postgres: _PublicationFixture,
) -> None:
    fixture = publication_postgres
    prepared = _prepare_source(fixture)
    before = _load_draft(fixture, prepared)
    with fixture.database.transaction() as transaction:
        renewed = fixture.drafts.renew_generation_claim(
            transaction,
            owner_id=OWNER,
            draft_id=prepared.draft_id,
            expected_revision=prepared.source_state_revision,
            claim_id=prepared.claim_id,
            lease_seconds=1800,
        )
    assert renewed.state_revision == before.state_revision
    assert renewed.generation_claim_expires_at is not None
    assert before.generation_claim_expires_at is not None
    assert renewed.generation_claim_expires_at > before.generation_claim_expires_at
    with (
        pytest.raises(RepositoryConflictError, match="renewal fence"),
        fixture.database.transaction() as transaction,
    ):
        fixture.drafts.renew_generation_claim(
            transaction,
            owner_id=OWNER,
            draft_id=prepared.draft_id,
            expected_revision=prepared.source_state_revision,
            claim_id=str(uuid.uuid4()),
        )

    execution = _attempt(fixture, prepared.operation_binding)
    kwargs = _begin_kwargs(prepared, execution)
    with (
        pytest.raises(RepositoryValidationError, match="canonical"),
        fixture.database.transaction() as transaction,
    ):
        fixture.publications.begin_intent(
            transaction,
            **{
                **kwargs,
                "staging_relative_path": (
                    f".staging/{prepared.draft_uuid}/"
                    f"{prepared.source_state_revision}/{prepared.publication_id}"
                ),
            },
        )
    with fixture.database.transaction() as transaction:
        intent = fixture.publications.begin_intent(transaction, **kwargs)
    with fixture.database.transaction() as transaction:
        replay = fixture.publications.begin_intent(transaction, **kwargs)
    assert replay.replayed and replay.publication == intent.publication

    unrelated = _attempt(
        fixture,
        replace(
            prepared.operation_binding,
            idempotency_key=f"unrelated-{uuid.uuid4()}",
            normalized_input_digest="d" * 64,
        ),
    )
    with (
        pytest.raises(RepositoryConflictError, match="operation attempt"),
        fixture.database.transaction() as transaction,
    ):
        fixture.publications.begin_intent(
            transaction,
            **{**kwargs, "attempt": unrelated},
        )

    for statement, expected_message in (
        (
            "UPDATE user_agent_revision SET state = 'active' WHERE revision_id = %s",
            "revision replay",
        ),
        (
            "UPDATE user_agent_revision SET compatibility_state = 'incompatible' "
            "WHERE revision_id = %s",
            "revision replay",
        ),
    ):
        with (
            pytest.raises(RuntimeError, match="rollback exploit probe"),
            fixture.database.transaction() as transaction,
        ):
            transaction.execute(statement, (prepared.revision_id,))
            with pytest.raises(RepositoryConflictError, match=expected_message):
                fixture.publications.begin_intent(transaction, **kwargs)
            raise RuntimeError("rollback exploit probe")

    with (
        pytest.raises(RuntimeError, match="rollback exploit probe"),
        fixture.database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE draft_agents SET generation_claim_expires_at = "
            "clock_timestamp() - interval '1 second' WHERE id = %s",
            (prepared.draft_id,),
        )
        with pytest.raises(RepositoryConflictError, match="generation claim"):
            fixture.publications.begin_intent(transaction, **kwargs)
        raise RuntimeError("rollback exploit probe")

    with fixture.database.transaction() as transaction:
        fixture.publications.renew_generation_claim(
            transaction,
            expected=intent.publication,
            attempt=execution,
            lease_seconds=600,
        )


def test_two_postgres_reconcilers_converge_on_one_designated_child_attempt(
    publication_postgres: _PublicationFixture,
) -> None:
    fixture = publication_postgres
    seeded = _seed(fixture)
    _terminalize(fixture, seeded.attempt, state=OperationState.FAILED)

    with fixture.database.transaction() as transaction:
        revision = fixture.agents.get_revision(
            transaction,
            owner_id=OWNER,
            agent_id=AGENT_ID,
            revision_id=seeded.prepared.revision_id,
        )
    assert revision is not None
    child_binding = generated_agent_publication_recovery_operation_binding(
        seeded.publication,
        revision,
    )
    wrong_child = _attempt(
        fixture,
        replace(
            child_binding,
            idempotency_key=f"wrong-{uuid.uuid4()}",
            parent_operation_id=None,
        ),
    )
    with (
        pytest.raises(RepositoryConflictError, match="recovery"),
        fixture.database.transaction() as transaction,
    ):
        fixture.publications.rebind_recovery_attempt(
            transaction,
            expected=seeded.publication,
            new_attempt=wrong_child,
            lease_seconds=600,
        )

    recovery = _attempt(fixture, child_binding)
    second_scope = ExitStack()
    second_database = second_scope.enter_context(concurrent_database(fixture))
    second_repository = GeneratedAgentPublicationRepository()
    barrier = threading.Barrier(2)
    successes: list[DraftPublicationRecord] = []
    failures: list[BaseException] = []
    guard = threading.Lock()

    def rebind(
        database: PlaneDatabase,
        repository: GeneratedAgentPublicationRepository,
    ) -> None:
        try:
            barrier.wait(timeout=10)
            with database.transaction() as transaction:
                result = repository.rebind_recovery_attempt(
                    transaction,
                    expected=seeded.publication,
                    new_attempt=recovery,
                    lease_seconds=600,
                )
            with guard:
                successes.append(result)
        except BaseException as exc:  # pragma: no cover
            with guard:
                failures.append(exc)

    threads = (
        threading.Thread(target=rebind, args=(fixture.database, fixture.publications)),
        threading.Thread(target=rebind, args=(second_database, second_repository)),
    )
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert all(not thread.is_alive() for thread in threads)
        assert failures == []
        assert len(successes) == 2
        assert successes[0] == successes[1]
        winner = successes[0]
        assert winner.state_revision == seeded.publication.state_revision + 1
        assert winner.operation_id == str(recovery.operation_id)
        assert winner.operation_execution_generation == recovery.execution_generation
        assert winner.source_state_revision == seeded.publication.source_state_revision
        assert winner.generation_claim_id == seeded.publication.generation_claim_id
        with fixture.database.transaction() as transaction:
            stored = fixture.publications.get_by_source(
                transaction,
                owner_id=OWNER,
                draft_uuid=seeded.publication.draft_uuid,
                source_state_revision=seeded.publication.source_state_revision,
            )
            fixture.publications.assert_current_attempt(
                transaction,
                expected=winner,
                attempt=recovery,
            )
        assert stored == winner
    finally:
        second_scope.close()


def test_publication_failure_is_atomic_replayable_and_preserves_owner_fences(publication_postgres):
    fixture = publication_postgres
    seeded = _seed(fixture)
    with fixture.database.transaction() as tx:
        staged = fixture.publications.mark_staged(
            tx, expected=seeded.publication, attempt=seeded.attempt
        )
        failed = fixture.publications.fail(
            tx,
            expected=staged,
            attempt=seeded.attempt,
            failure_code="validation_failed",
            safe_error_message="validation failed",
        )
        assert failed.state == "failed"
        assert (
            fixture.publications.fail(
                tx,
                expected=staged,
                attempt=seeded.attempt,
                failure_code="validation_failed",
                safe_error_message="validation failed",
            )
            == failed
        )
        draft = fixture.drafts.get_draft(tx, owner_id=OWNER, draft_id=seeded.prepared.draft_id)
        revision = fixture.agents.get_revision(
            tx,
            owner_id=OWNER,
            agent_id=AGENT_ID,
            revision_id=seeded.prepared.revision_id,
        )
        assert draft.status == "error" and draft.generation_claim_id is None
        assert revision.state == "failed" and revision.failure_code == "validation_failed"
        with pytest.raises(RepositoryNotFoundError):
            fixture.publications.fail(
                tx,
                expected=replace(staged, owner_id="another-owner"),
                attempt=seeded.attempt,
                failure_code="validation_failed",
                safe_error_message="validation failed",
            )


@pytest.mark.parametrize("fault", ["abort_publication", "ignore_draft", "delete_publication"])
def test_sqlite_transition_failures_restore_draft_and_publication_inside_caller(
    publication_postgres,
    fault,
):
    import sqlite3

    fixture = publication_postgres
    with fixture.database.transaction() as tx:
        if not tx.dialect.is_sqlite:
            pytest.skip("SQLite native trigger fault; PostgreSQL CTE atomicity has its own tests")
    seeded = _seed(fixture)
    with fixture.database.transaction() as tx:
        staged = fixture.publications.mark_staged(
            tx, expected=seeded.publication, attempt=seeded.attempt
        )
    before = _load_draft(fixture, seeded.prepared)
    with fixture.database.transaction() as tx:
        if fault == "abort_publication":
            tx.execute(
                "CREATE TRIGGER reject_publication BEFORE UPDATE ON draft_artifact_publication "
                "BEGIN SELECT RAISE(ABORT,'publication rejected'); END"
            )
            expected_error = sqlite3.IntegrityError
        elif fault == "ignore_draft":
            tx.execute(
                "CREATE TRIGGER reject_draft BEFORE UPDATE ON draft_agents "
                "BEGIN SELECT RAISE(IGNORE); END"
            )
            expected_error = RepositoryConflictError
        else:
            tx.execute(
                "CREATE TRIGGER remove_publication AFTER UPDATE ON draft_agents "
                "BEGIN DELETE FROM draft_artifact_publication; END"
            )
            expected_error = RepositoryConflictError
        with pytest.raises(expected_error):
            fixture.publications.mark_validated(
                tx,
                expected=staged,
                attempt=seeded.attempt,
                artifact_digest=ARTIFACT_DIGEST,
                manifest_digest=seeded.prepared.bundle.manifest_sha256,
                generation_result=RESULT_METADATA,
            )
        assert (
            fixture.drafts.get_draft(tx, owner_id=OWNER, draft_id=seeded.prepared.draft_id)
            == before
        )
        assert (
            fixture.publications.get_by_target_revision(
                tx,
                owner_id=OWNER,
                target_agent_id=AGENT_ID,
                target_revision_id=seeded.prepared.revision_id,
            )
            == staged
        )
        assert tx.fetch_one("SELECT 1 AS usable")["usable"] == 1
