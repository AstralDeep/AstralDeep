"""Real-PostgreSQL proof that every public repository's mutation rolls back completely
when the caller aborts its transaction; exercises the full astralplane.repositories
catalog against one schema.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from astralplane import api
from astralplane.audit_retention import HMACAnchorAuthenticator
from astralplane.authority import (
    AgentAuthorityBinding,
    AuthorityPopulation,
)
from astralplane.contracts import OutboxEntry, Transaction
from astralplane.database.pool import ConnectionPool
from astralplane.database.transaction import PlaneDatabase
from astralplane.immutable_bundle_store import FinalizedBundle, canonical_bundle_digest
from astralplane.repositories.atlas_links import AtlasLinkSetCommand
from astralplane.repositories.atlas_pages import AtlasPageCreateCommand, AtlasPageEditCommand
from astralplane.repositories.audit import AuditEvent
from astralplane.repositories.background_tasks import (
    BackgroundTaskRecord,
    BackgroundTaskStatus,
)
from astralplane.repositories.computer_use import ComputerSession
from astralplane.repositories.generated_agent_publications import (
    GENERATED_AGENT_BUNDLE_CONTRACT,
    generated_agent_publication_operation_binding,
    generated_agent_publication_paths,
)
from astralplane.repositories.harness_cleanup import HarnessCleanupProfile
from astralplane.repositories.maintenance import (
    MaintenanceInputRecord,
    MaintenanceUnitRecord,
)
from astralplane.repositories.observations import ObservationCaptureCommand
from astralplane.repositories.personalization_graph import ShortTermSignalRecord
from astralplane.repositories.preferences import FeedbackRecord
from astralplane.repositories.quality_audit import QualityTestRunRecord
from astralplane.repositories.remote import RemoteMachine
from astralplane.repositories.remote_proposals import RemoteOperationProposalRecord
from astralplane.repositories.scheduler import ScheduledJob
from astralplane.repositories.tracked_jobs import TrackedJobRecord
from astralplane.repositories.voice import VoiceSessionCreate
from astralplane.repositories.work_admission import (
    AcceptedAdmission,
    AdmissionClass,
    AdmissionClassConfig,
    OperationOwner,
    OperationRequest,
    OwnerScope,
)
from astralplane.repositories.workspaces import CanvasComponentRecord
from tests.fixtures.migrated_template import DatabaseTemplate, bound_clone

_NOW = datetime(2026, 8, 14, 18, tzinfo=UTC)
_OWNER = "rollback-owner"
_SUPPORT_CHAT = "rollback-support-chat"
_SUPPORT_AGENT = "rollback-support-agent"
_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64
_AUDIT_EVENT_ID = "74000000-0000-4000-8000-000000000001"
_CHAT_QUEUE_ITEM = "74000000-0000-4000-8000-000000000004"
_RETENTION_EVENT_1 = "74000000-0000-4000-8000-000000000002"
_RETENTION_EVENT_2 = "74000000-0000-4000-8000-000000000003"
_GENERATED_PUBLICATION_ID = "74000000-0000-4000-8000-000000000020"


class _ForcedCallerRollbackError(RuntimeError):
    pass


class _DedicatedDriverPool:
    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.borrowed = False

    def getconn(self) -> Any:
        if self.borrowed:
            raise RuntimeError("integration connection is already borrowed")
        self.borrowed = True
        return self.connection

    def putconn(self, connection: Any, *, close: bool = False) -> None:
        if connection is not self.connection or not self.borrowed or close:
            raise RuntimeError("integration connection was returned in an invalid state")
        self.borrowed = False

    def closeall(self) -> None:
        return None


@dataclass(slots=True)
class _CatalogDatabase:
    connection: Any
    schema: str
    pool: ConnectionPool
    database: PlaneDatabase
    catalog: api.RepositoryCatalog


def _write_projects(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    project_id = "11111111-1111-4111-8111-111111110082"
    catalog.projects.create(
        transaction,
        owner_id=_OWNER,
        project_id=project_id,
        name="Rollback project",
        position=0,
        created_at=10,
    )
    catalog.projects.attach_chat(
        transaction, owner_id=_OWNER, project_id=project_id, chat_id=_SUPPORT_CHAT
    )


def _observe_projects(transaction: Transaction) -> tuple[int, int]:
    return (
        _count("chat_project", "owner_id", _OWNER)(transaction),
        _count("chat_project_membership", "owner_id", _OWNER)(transaction),
    )


def _observe_observations(transaction: Transaction) -> tuple[int, int]:
    observation = _count(
        "tool_observation", "observation_id", "11111111-1111-4111-8111-111111110081"
    )(transaction)
    page = _count(
        "tool_observation_page", "observation_id", "11111111-1111-4111-8111-111111110081"
    )(transaction)
    return (observation, page)


def _write_observations(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.observations.capture(
        transaction,
        command=ObservationCaptureCommand(
            owner_id=_OWNER,
            conversation_id="rollback-conversation",
            observation_id="11111111-1111-4111-8111-111111110081",
            operation_id="rollback-operation",
            source_refs=("synthetic/rollback",),
            seal="rollback-seal-v1",
            content=b"synthetic-opaque-observation-ciphertext",
            created_at=10,
            expires_at=20,
        ),
    )


@pytest.fixture(scope="module")
def catalog_database(migrated_template: DatabaseTemplate) -> Iterator[_CatalogDatabase]:
    with bound_clone(migrated_template) as clone:
        catalog = api.create_repository_catalog()
        _seed_support_rows(clone.database, catalog)
        yield _CatalogDatabase(clone.connection, clone.schema, clone.pool, clone.database, catalog)


def _seed_support_rows(database: PlaneDatabase, catalog: api.RepositoryCatalog) -> None:
    with database.transaction() as transaction:
        catalog.identity.upsert_identity(
            transaction,
            owner_id=_OWNER,
            observed_at=1,
            email="rollback@example.test",
            username="rollback",
            display_name="Rollback Owner",
            roles=("member",),
        )
        catalog.agents.create_agent(
            transaction,
            agent_id=_SUPPORT_AGENT,
            owner_id=_OWNER,
            display_name="Rollback support agent",
            observed_at=1,
        )
        catalog.history.conversations.create(
            transaction,
            conversation_id=_SUPPORT_CHAT,
            owner_id=_OWNER,
            title="Rollback support",
            agent_id=_SUPPORT_AGENT,
            created_at=1,
        )


Write = Callable[[api.RepositoryCatalog, Transaction], None]
Observe = Callable[[Transaction], object]
Prepare = Callable[[_CatalogDatabase], None]


@dataclass(frozen=True, slots=True)
class RollbackCase:
    key: str
    write: Write
    observe: Observe
    during: object
    after: object
    prepare: Prepare | None = None


def _count(
    table: str,
    column: str,
    value: object,
    *,
    extra_sql: str = "",
    extra_parameters: tuple[object, ...] = (),
) -> Observe:
    assert table.replace("_", "").isalnum()
    assert column.replace("_", "").isalnum()

    def observe(transaction: Transaction) -> object:
        row = transaction.fetch_one(
            f"SELECT COUNT(*) AS value FROM {table} WHERE {column} = %s{extra_sql}",
            (value, *extra_parameters),
        )
        assert row is not None
        return int(row["value"])

    return observe


def _audit_authenticate(key_id: str, payload: bytes) -> bytes:
    assert key_id == "rollback-audit-key"
    return hmac.new(b"rollback-audit-secret", payload, hashlib.sha256).digest()


def _audit_event(event_id: str, chain_id: str = _OWNER) -> AuditEvent:
    return AuditEvent(
        event_id=event_id,
        chain_id=chain_id,
        auth_principal="rollback-principal",
        agent_id=_SUPPORT_AGENT,
        event_class="tool_call",
        action_type="tool.execute",
        description="rollback contract evidence",
        conversation_id=_SUPPORT_CHAT,
        correlation_id="74000000-0000-4000-8000-000000000099",
        outcome="success",
        outcome_detail=None,
        inputs_json="{}",
        outputs_json="{}",
        artifact_pointers_json="[]",
        started_at=_NOW,
        completed_at=_NOW,
        key_id="rollback-audit-key",
    )


def _write_agents(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.agents.create_agent(
        transaction,
        agent_id="rollback-agent",
        owner_id=_OWNER,
        display_name="Rolled back agent",
        observed_at=10,
    )


def _write_assignments(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    from dataclasses import replace

    from tests.repositories.test_assignments import definition

    grant_id = "79000000-0000-4000-8000-000000000001"
    transaction.execute(
        "INSERT INTO user_offline_grant(id,user_id,refresh_token_enc,issued_at,expires_at) "
        "VALUES(%s,%s,%s,1,9999999999999)",
        (grant_id, _OWNER, b"rollback-test-opaque"),
    )
    catalog.assignments.create_assignment(
        transaction,
        owner_id=_OWNER,
        assignment_id="79000000-0000-4000-8000-000000000002",
        submission_id="79000000-0000-4000-8000-000000000003",
        submission_digest=_DIGEST_A,
        definition=replace(definition(), offline_grant_id=grant_id),
    )


def _write_artifacts(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.artifacts.materializations.begin_pending_materialization(
        transaction,
        attachment_id="rollback-attachment",
        owner_id=_OWNER,
        filename="rollback.txt",
        category="text",
        extension="txt",
        storage_locator=f"{_OWNER}/rollback-attachment/rollback.txt",
        storage_key="rollback-attachment/rollback.txt",
        max_bytes=8,
        created_at=10,
        lease_id="rollback-lease",
        lease_seconds=300,
    )


def _write_attachment_parsers(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.attachment_parsers.claim_pending(
        transaction,
        owner_id=_OWNER,
        gap_fingerprint="rollback-parser-gap",
        category="data",
        extension="avro",
        draft_agent_id=None,
        source_attachment_id=None,
        source_conversation_id=None,
        claimed_at=10,
    )


def _write_audit(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.audit.append(
        transaction,
        _audit_event(_AUDIT_EVENT_ID),
        _audit_authenticate,
    )


def _prepare_audit_retention(fixture: _CatalogDatabase) -> None:
    with fixture.database.transaction() as transaction:
        fixture.catalog.audit.append(
            transaction,
            _audit_event(_RETENTION_EVENT_1, "rollback-retention-chain"),
            _audit_authenticate,
        )
        fixture.catalog.audit.append(
            transaction,
            _audit_event(_RETENTION_EVENT_2, "rollback-retention-chain"),
            _audit_authenticate,
        )


def _write_audit_retention(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    authenticator = HMACAnchorAuthenticator(
        lambda key_id: (
            b"rollback-anchor-secret-material-32" if key_id == "rollback-anchor-key" else b""
        )
    )
    catalog.audit_retention.prune_prefix(
        transaction,
        chain_id="rollback-retention-chain",
        first_retained_sequence=2,
        anchor_id="rollback-retention-anchor",
        policy_digest=hashlib.sha256(b"rollback-policy").digest(),
        created_at=_NOW,
        key_id="rollback-anchor-key",
        authenticator=authenticator,
    )


def _observe_audit_retention(transaction: Transaction) -> object:
    anchor = transaction.fetch_one(
        "SELECT COUNT(*) AS value FROM audit_retention_anchor WHERE anchor_id = %s",
        ("rollback-retention-anchor",),
    )
    prefix = transaction.fetch_one(
        "SELECT COUNT(*) AS value FROM audit_events WHERE event_id = %s",
        (_RETENTION_EVENT_1,),
    )
    assert anchor is not None and prefix is not None
    return int(anchor["value"]), int(prefix["value"])


def _write_authority(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.authority.create_binding(
        transaction,
        AgentAuthorityBinding.provisioning_intent(
            binding_id="rollback-authority-binding",
            owner_id=_OWNER,
            agent_id=_SUPPORT_AGENT,
            runtime_id="rollback-runtime",
            runtime_generation=1,
            population=AuthorityPopulation.BYO_USER,
            tenant_id="rollback-tenant",
            envelope_id="rollback-envelope",
            policy_digest="sha256:" + _DIGEST_A,
            machine_digest="sha256:" + _DIGEST_B,
            config_epoch=1,
            capabilities=("tool:rollback",),
            created_at=_NOW,
        ),
    )


def _write_background_tasks(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.background_tasks.create(
        transaction,
        BackgroundTaskRecord(
            task_id="rollback-background-task",
            owner_id=_OWNER,
            conversation_id=_SUPPORT_CHAT,
            kind="async_chat",
            status=BackgroundTaskStatus.QUEUED,
            title="Rollback background task",
            created_at=_NOW,
        ),
    )


def _write_chat_queue(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.chat_queue.enqueue(
        transaction,
        owner_id=_OWNER,
        chat_id=_SUPPORT_CHAT,
        text="rollback follow-up",
        created_at=10,
        item_id=_CHAT_QUEUE_ITEM,
    )


def _write_chat_read_receipts(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.chat_read_receipts.mark_seen(
        transaction,
        owner_id=_OWNER,
        chat_id=_SUPPORT_CHAT,
        last_seen_position=3,
        expected_revision=0,
    )


_MESH_GENESIS = b"rollback-genesis"


def _mesh_reference(data: bytes) -> str:
    import hashlib

    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _write_derived_chat(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    event = _mesh_reference(b"rollback-derived-chat")
    catalog.derived_chat.upsert_chat(
        transaction,
        owner_id=_OWNER,
        event_id=event,
        chat_id=event,
        title="Rollback derived chat",
        agent_id=None,
        created_at=10,
    )


def _write_event_log(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    mesh = catalog.event_roster.create_mesh(
        transaction, owner_id=_OWNER, kind="genesis", signed_bytes=_MESH_GENESIS, recorded_at=1
    ).mesh_id
    header = b"rollback-header"
    catalog.event_log.append_event(
        transaction,
        owner_id=_OWNER,
        mesh_id=mesh,
        feed_id="rollback-feed",
        seq=1,
        event_id=_mesh_reference(header),
        prev_hash=None,
        clock_ms=1,
        clock_n=0,
        scope="rollback-scope",
        event_type="chat.message",
        labels={},
        key_id="epoch-1",
        body_hash=_mesh_reference(b"rollback-body"),
        header_bytes=header,
        header_sealed=b"sealed",
        recorded_at=2,
    )


def _write_event_roster(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.event_roster.create_mesh(
        transaction, owner_id=_OWNER, kind="genesis", signed_bytes=_MESH_GENESIS, recorded_at=1
    )


def _write_learning_ledger(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    mesh = catalog.event_roster.create_mesh(
        transaction, owner_id=_OWNER, kind="genesis", signed_bytes=_MESH_GENESIS, recorded_at=1
    ).mesh_id
    catalog.learning_ledger.record_proposal(
        transaction,
        owner_id=_OWNER,
        mesh_id=mesh,
        event_id=_mesh_reference(b"rollback-proposal"),
        item_kind="memory",
        content_hash=_mesh_reference(b"rollback-content"),
        scope="rollback-scope",
        evidence=[_mesh_reference(b"rollback-evidence")],
        recorded_at=2,
    )


def _write_runs(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    mesh = catalog.event_roster.create_mesh(
        transaction, owner_id=_OWNER, kind="genesis", signed_bytes=_MESH_GENESIS, recorded_at=1
    ).mesh_id
    catalog.runs.record_lease(
        transaction,
        owner_id=_OWNER,
        mesh_id=mesh,
        run_id="rollback-run",
        kind="take",
        executor_feed_id="rollback-feed",
        state="running",
        event_id=_mesh_reference(b"rollback-lease"),
        recorded_at=2,
    )


def _write_chat_steps(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.chat_steps.create_step(
        transaction,
        step_id="rollback-chat-step",
        owner_id=_OWNER,
        conversation_id=_SUPPORT_CHAT,
        turn_message_id=None,
        kind="phase",
        name="rollback",
        args_truncated=None,
        args_was_truncated=False,
        started_at=10,
    )


def _write_conversation_files(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.conversation_files.add_mapping(
        transaction,
        owner_id=_OWNER,
        conversation_id=_SUPPORT_CHAT,
        original_name="rollback.txt",
        backend_path="uploads/rollback.txt",
        uploaded_at=10,
    )


def _write_credentials(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.credentials.upsert_credential(
        transaction,
        owner_id=_OWNER,
        agent_id=_SUPPORT_AGENT,
        credential_key="rollback",
        encrypted_value="opaque-rollback-ciphertext",
        updated_at=10,
    )


def _write_draft_agents(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.draft_agents.create_draft(
        transaction,
        draft_id="rollback-draft",
        owner_id=_OWNER,
        agent_name="Rollback Draft",
        agent_slug="rollback-draft",
        description="Rollback evidence",
        observed_at=10,
        draft_uuid="10000000-0000-4000-8000-000000000074",
        target_agent_id="rollback-draft-target",
    )


def _write_generated_agent_publications(
    catalog: api.RepositoryCatalog,
    transaction: Transaction,
) -> None:
    draft_id = "rollback-generated-publication-draft"
    draft_uuid = "74000000-0000-4000-8000-000000000021"
    claim_id = "74000000-0000-4000-8000-000000000022"
    agent_id = "rollback-generated-publication-agent"
    revision_id = "74000000-0000-4000-8000-000000000023"
    promotion_token = "74000000-0000-4000-8000-000000000024"
    files = {
        "agent_main.py": "rollback main\n",
        "astralprims_ui.py": "rollback ui\n",
        "protected_executor.py": "rollback executor\n",
        "mcp_tools.py": "rollback tools\n",
    }
    bundle_digest = canonical_bundle_digest(files, GENERATED_AGENT_BUNDLE_CONTRACT)
    lock_digest = "c" * 64
    manifest = {
        "agent_id": agent_id,
        "agent_name": "Rollback generated publication",
        "bundle_sha256": bundle_digest,
        "constitution_version": "0.1.0",
        "description": "Synthetic rollback fixture",
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
        "required_runtime_lock_sha256": lock_digest,
        "revision_id": revision_id,
        "runtime_contract_version": 3,
    }
    bundle = FinalizedBundle(
        contract=GENERATED_AGENT_BUNDLE_CONTRACT,
        files=files,
        bundle_sha256=bundle_digest,
        manifest=manifest,
        manifest_json=(
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ),
    )
    catalog.draft_agents.create_draft(
        transaction,
        draft_id=draft_id,
        owner_id=_OWNER,
        agent_name="Rollback generated publication",
        agent_slug="rollback-generated-publication",
        description="Synthetic rollback fixture",
        observed_at=10,
        draft_uuid=draft_uuid,
        target_agent_id=agent_id,
    )
    catalog.agents.create_agent(
        transaction,
        agent_id=agent_id,
        owner_id=_OWNER,
        display_name="Rollback generated publication",
        observed_at=10,
        draft_id=draft_id,
    )
    claimed = catalog.draft_agents.claim_generation(
        transaction,
        owner_id=_OWNER,
        draft_id=draft_id,
        expected_revision=0,
        claim_id=claim_id,
        lease_seconds=300,
    )
    binding = generated_agent_publication_operation_binding(
        owner_id=_OWNER,
        publication_id=_GENERATED_PUBLICATION_ID,
        draft_uuid=draft_uuid,
        source_state_revision=claimed.state_revision,
        generation_claim_id=claim_id,
        target_agent_id=agent_id,
        target_revision_id=revision_id,
        bundle=bundle,
        runtime_contract_version=3,
        release_lock_digest=lock_digest,
        promotion_token=promotion_token,
    )
    catalog.work_admission.bind_configs(catalog.work_admission.load_existing_configs(transaction))
    accepted = catalog.work_admission.submit(
        transaction,
        OperationRequest(
            operation_kind=binding.operation_kind,
            admission_class=AdmissionClass.INTERACTIVE,
            owner=OperationOwner(OwnerScope.USER, _OWNER, None),
            submission_id=uuid.UUID("74000000-0000-4000-8000-000000000025"),
            idempotency_namespace=binding.idempotency_namespace,
            idempotency_key=binding.idempotency_key,
            normalized_input_digest=binding.normalized_input_digest,
            chat_id=None,
            parent_operation_id=binding.parent_operation_id,
            connection_generation=None,
            request_generation=uuid.UUID("74000000-0000-4000-8000-000000000026"),
        ),
        now=_NOW,
        retention=timedelta(days=1),
        slot_lease=timedelta(minutes=5),
    )
    assert isinstance(accepted, AcceptedAdmission)
    operation_claim = catalog.work_admission.claim_operation(
        transaction,
        AdmissionClass.INTERACTIVE,
        accepted.operation_id,
        now=_NOW,
        retention=timedelta(days=1),
        slot_lease=timedelta(minutes=5),
    )
    assert operation_claim is not None
    paths = generated_agent_publication_paths(
        draft_uuid=draft_uuid,
        source_state_revision=claimed.state_revision,
        publication_id=_GENERATED_PUBLICATION_ID,
        target_agent_id=agent_id,
        target_revision_id=revision_id,
    )
    catalog.generated_agent_publications.begin_intent(
        transaction,
        owner_id=_OWNER,
        publication_id=_GENERATED_PUBLICATION_ID,
        draft_uuid=draft_uuid,
        source_state_revision=claimed.state_revision,
        generation_claim_id=claim_id,
        target_agent_id=agent_id,
        target_revision_id=revision_id,
        staging_relative_path=paths.staging_relative_path,
        revision_relative_path=paths.revision_relative_path,
        bundle=bundle,
        runtime_contract_version=3,
        release_lock_digest=lock_digest,
        promotion_token=promotion_token,
        attempt=operation_claim.fence,
    )


def _write_encrypted_llm_config(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.encrypted_llm_config.upsert_user(
        transaction,
        owner_id=_OWNER,
        provider="openai",
        base_url="https://example.invalid/v1",
        model="rollback-model",
        api_key_ciphertext="opaque-ciphertext",
    )


def _write_encrypted_typesafe_credential(
    catalog: api.RepositoryCatalog, transaction: Transaction
) -> None:
    catalog.encrypted_typesafe_credential.upsert_user(
        transaction,
        owner_id=_OWNER,
        api_key_ciphertext="opaque-ciphertext",
        key_fingerprint="0123456789ab",
        verified_at=datetime(2026, 9, 17, tzinfo=UTC),
    )


_FRAMEWORK_ISSUER: dict[str, str] = {}


def _prepare_framework_credentials(fixture: _CatalogDatabase) -> None:
    from astralplane.repositories.history import SessionRecord, SessionRepository

    with fixture.database.transaction() as transaction:
        now = int(transaction.fetch_one("SELECT clock_timestamp() AS now")["now"].timestamp())
        record = SessionRepository().put(
            transaction,
            SessionRecord(
                "rollback-framework-session",
                _OWNER,
                "synthetic-encrypted-access",
                "synthetic-encrypted-refresh",
                now,
                now + 3600,
                now,
                False,
                now,
            ),
        )
    _FRAMEWORK_ISSUER[_OWNER] = record.incarnation_id


def _write_framework_credentials(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.framework_credentials.issue(
        transaction,
        owner_id=_OWNER,
        credential_id="5b8e0000-0000-4000-8000-0000000f8acc",
        name="Rollback credential",
        scopes=("operations.submit", "operations.read"),
        token_hash="c" * 64,
        token_prefix="afk_roll",
        issuer_kind="session_incarnation",
        issuer_reference=_FRAMEWORK_ISSUER[_OWNER],
        max_admissions=1,
        ttl_seconds=3600,
    )


def _write_history(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.history.conversations.create(
        transaction,
        conversation_id="rollback-history-chat",
        owner_id=_OWNER,
        title="Rolled back conversation",
        agent_id=_SUPPORT_AGENT,
        created_at=10,
    )


def _prepare_harness_cleanup(fixture: _CatalogDatabase) -> None:
    with fixture.database.transaction() as transaction:
        fixture.catalog.draft_agents.create_draft(
            transaction,
            draft_id="__verif__rollback_cleanup_draft",
            owner_id="__verif__rollback_cleanup_owner",
            agent_name="Cleanup target",
            agent_slug="cleanup-target",
            description="Committed cleanup target",
            observed_at=10,
        )


def _write_harness_cleanup(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    report = catalog.harness_cleanup.purge_run(
        transaction,
        profile=HarnessCleanupProfile.VERIFICATION,
        run_id="rollback_cleanup",
    )
    assert report.total_deleted == 1


def _write_identity(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.identity.upsert_identity(
        transaction,
        owner_id="rollback-new-owner",
        observed_at=10,
        email="new-owner@example.test",
        username="rollback-new-owner",
        display_name="Rollback New Owner",
        roles=("member",),
    )


def _write_stop_epochs(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.stop_epochs.engage(
        transaction,
        owner_id=_OWNER,
        expected_revision=0,
        reason="synthetic rollback",
        actor_id=_OWNER,
        at=_NOW,
    )


def _write_mesh_enrollment(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.mesh_enrollment.bootstrap_mesh(
        transaction,
        mesh_id="11111111-1111-4111-8111-111111110075",
        owner_id=_OWNER,
        display_name="rollback mesh",
        bootstrap_member_id="22222222-2222-4222-8222-222222220075",
        bootstrap_member_kind="device",
    )


def _write_verification_credentials(
    catalog: api.RepositoryCatalog, transaction: Transaction
) -> None:
    catalog.verification_credentials.enroll_credential(
        transaction,
        owner_id=_OWNER,
        credential_id="11111111-1111-4111-8111-111111110076",
        device_id="rollback-mac-device",
        algorithm="es256",
        key_format="spki",
        public_key="0" * 64,
    )
    catalog.verification_credentials.consume_challenge(
        transaction,
        owner_id=_OWNER,
        challenge_digest="b" * 64,
        consumption_id="22222222-2222-4222-8222-222222220076",
    )


def _observe_verification_credentials(transaction: Transaction) -> object:
    return (
        transaction.fetch_one(
            "SELECT COUNT(*) AS value FROM verification_credential "
            "WHERE credential_id = %s",
            ("11111111-1111-4111-8111-111111110076",),
        )["value"],
        transaction.fetch_one(
            "SELECT COUNT(*) AS value FROM verification_challenge_consumption "
            "WHERE consumption_id = %s",
            ("22222222-2222-4222-8222-222222220076",),
        )["value"],
    )


def _write_knowledge(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.knowledge.interactions.record_for_owner(
        transaction,
        owner_id=_OWNER,
        conversation_id=_SUPPORT_CHAT,
        agent_id=_SUPPORT_AGENT,
        tool_name="rollback.tool",
        success=True,
        error_message=None,
        response_time_ms=1,
        created_at=10,
    )


def _write_maintenance(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.maintenance.create_unit(
        transaction,
        MaintenanceUnitRecord(
            unit_id="11111111-1111-4111-8111-111111110074",
            unit_kind="agent_synthesis",
            owner_id=_OWNER,
            scope_key=_SUPPORT_AGENT,
            idempotency_key="rollback-maintenance",
            max_attempts=2,
            output_generation="22222222-2222-4222-8222-222222220074",
        ),
        inputs=(
            MaintenanceInputRecord(
                unit_id="11111111-1111-4111-8111-111111110074",
                input_kind="interaction",
                input_id="rollback-input",
                input_digest=_DIGEST_A,
            ),
        ),
    )


def _write_offline_grants(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.offline_grants.create_grant(
        transaction,
        grant_id="9ef050be-0d5f-4a82-b3cb-410de6d9074a",
        owner_id=_OWNER,
        agent_id=_SUPPORT_AGENT,
        encrypted_refresh_token=b"opaque-rollback-token",
        issued_at=10,
        expires_at=1000,
    )


def _write_outbox(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    payload = b'{"rollback":true}'
    catalog.outbox.enqueue(
        transaction,
        OutboxEntry(
            entry_id="rollback-outbox-entry",
            topic="rollback.contract",
            canonical_payload=payload,
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            idempotency_key="rollback-outbox-idempotency",
            available_at=_NOW,
        ),
    )


def _write_preferences(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.preferences.feedback.submit(
        transaction,
        FeedbackRecord(
            feedback_id="74000000-0000-4000-8000-000000000010",
            owner_id=_OWNER,
            conversation_id=_SUPPORT_CHAT,
            correlation_id="rollback-correlation",
            source_agent=_SUPPORT_AGENT,
            source_tool="rollback.tool",
            component_id="rollback-component",
            sentiment="positive",
            category="other",
            comment="rollback evidence",
            comment_safety="clean",
            comment_safety_reason="bounded test text",
            lifecycle="active",
            superseded_by=None,
            created_at=_NOW,
            updated_at=_NOW,
        ),
    )


def _write_personalization_graph(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.personalization_graph.create_signal(
        transaction,
        ShortTermSignalRecord(
            signal_id="74000000-0000-4000-8000-000000000011",
            owner_id=_OWNER,
            category="preference",
            value="rollback evidence",
            created_at=10,
        ),
    )


def _write_purge(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.purge.schedule_attachment_prefix(
        transaction,
        owner_id=_OWNER,
        attachment_id="rollback-purge-object",
        requested_at=_NOW,
        deleted_at=20,
    )


def _prepare_purge(fixture: _CatalogDatabase) -> None:
    with fixture.database.transaction() as transaction:
        transaction.execute(
            """
            INSERT INTO user_attachments (
                attachment_id, user_id, filename, content_type, category,
                extension, size_bytes, sha256, storage_path, created_at,
                deleted_at, materialization_state
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, 'ready'
            )
            ON CONFLICT (attachment_id) DO NOTHING
            """,
            (
                "rollback-purge-object",
                _OWNER,
                "object.bin",
                "application/octet-stream",
                "data",
                "bin",
                1,
                _DIGEST_A,
                f"{_OWNER}/rollback-purge-object/object.bin",
                10,
            ),
        )


def _write_quality_audit(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.quality_audit.create_run(
        transaction,
        QualityTestRunRecord(
            owner_id=_OWNER,
            run_id="rollback-quality-run",
            started_at=_NOW,
            finished_at=None,
            system_state={"composition": "rollback"},
            categories=("contract",),
            status="running",
        ),
    )


def _write_remote(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.remote.create_machine(
        transaction,
        RemoteMachine(
            machine_id="rollback-machine",
            owner_id=_OWNER,
            label="Rollback machine",
            address="rollback.example.invalid",
            port=22,
            username="runner",
            os_family="linux",
            role="cluster",
            host_key_type=None,
            host_key_fingerprint=None,
            host_key_blob=None,
            last_verdict=None,
            last_checked_at=None,
            created_at=10,
            updated_at=10,
        ),
    )


def _write_remote_operation_proposals(
    catalog: api.RepositoryCatalog, transaction: Transaction
) -> None:
    catalog.remote_operation_proposals.create(
        transaction,
        RemoteOperationProposalRecord(
            proposal_id="rollback-remote-proposal",
            owner_id=_OWNER,
            conversation_id=_SUPPORT_CHAT,
            machine_id="rollback-machine-reference",
            agent_id=_SUPPORT_AGENT,
            tool_name="remote.write_file",
            args_fingerprint=_DIGEST_A,
            arguments={"path": "/tmp/rollback"},
            summary="Rollback remote proposal",
            status="pending",
            created_at=10,
            expires_at=100,
        ),
    )


def _write_revocations(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.revocations.enqueue(
        transaction,
        owner_id=_OWNER,
        refresh_token_ciphertext="opaque-rollback-refresh-token",
        enqueued_at=10,
        client_id="astral-web",
    )


def _write_saved_components(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.saved_components.create(
        transaction,
        CanvasComponentRecord(
            row_id="rollback-saved-row",
            conversation_id=_SUPPORT_CHAT,
            owner_id=_OWNER,
            component_id="rollback-saved-component",
            payload={"type": "Text", "text": "rollback"},
            component_type="Text",
            title="Rollback",
            position=0,
            created_at=10,
            updated_at=10,
        ),
    )


def _write_scheduler(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.scheduler.create_job_definition(
        transaction,
        ScheduledJob(
            job_id="33333333-3333-4333-8333-333333330074",
            owner_id=_OWNER,
            name="Rollback schedule",
            instruction="Prove caller rollback",
            schedule_kind="cron",
            schedule_expression="0 9 * * *",
            timezone="UTC",
            status="active",
            next_run_at=100,
            created_at=10,
            updated_at=10,
        ),
    )


def _write_share_grants(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.share_grants.create_grant(
        transaction,
        token_sha256=_DIGEST_B,
        owner_id=_OWNER,
        chat_id=_SUPPORT_CHAT,
        scope="chat",
        component_id=None,
        snapshot_html="<section>rollback</section>",
        snapshot_json={"type": "Text", "text": "rollback"},
        expires_at=_NOW + timedelta(hours=1),
    )


def _write_tool_policy_state(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.tool_policy_state.set_agent_disabled(
        transaction,
        owner_id=_OWNER,
        agent_id=_SUPPORT_AGENT,
        disabled=True,
        updated_at=10,
    )


def _write_tracked_jobs(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.tracked_jobs.create(
        transaction,
        TrackedJobRecord(
            tracked_job_id="rollback-tracked-job",
            owner_id=_OWNER,
            machine_id="rollback-machine-reference",
            scheduler_job_id="rollback-scheduler-reference",
            conversation_id=_SUPPORT_CHAT,
            job_name="Rollback tracked job",
            created_at=10,
        ),
    )


def _write_tutorials(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.tutorials.create_with_revision(
        transaction,
        slug="rollback-tutorial",
        audience="user",
        display_order=7400,
        target_kind="static",
        target_key="rollback",
        title="Rollback tutorial",
        body="Caller rollback evidence",
        editor_id=_OWNER,
        observed_at=_NOW,
    )


def _write_voice(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.voice.create_session(
        transaction,
        VoiceSessionCreate(
            session_id="74000000-0000-4000-8000-000000000012",
            owner_id=_OWNER,
            activation_id="74000000-0000-4000-8000-000000000013",
            device_id="74000000-0000-4000-8000-000000000014",
            device_kind="web",
            transport="livekit",
            speech_backend="llm_factory",
            room_name="rollback-room",
            participant_identity="rollback-participant",
            visible_chat_id=_SUPPORT_CHAT,
            owner_connection_generation="10000000-0000-4000-8000-000000000074",
            control_binding_id="74000000-0000-4000-8000-000000000015",
            control_binding_expires_at=_NOW + timedelta(minutes=5),
            lease_expires_at=_NOW + timedelta(minutes=5),
            media_grant_nonce_hash=bytes.fromhex(_DIGEST_A),
            media_grant_issued_at=_NOW,
            media_grant_expires_at=_NOW + timedelta(minutes=1),
            started_at=_NOW,
        ),
    )


def _write_work_admission(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.work_admission.configure(
        transaction,
        (
            AdmissionClassConfig(
                class_name=AdmissionClass.GLOBAL,
                parent_class_name=None,
                active_limit=1,
                queue_limit=1,
                max_wait_ms=1000,
                config_revision="rollback-contract-v1",
            ),
        ),
    )


def _write_atlas_pages(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.atlas_pages.create_page(
        transaction,
        command=AtlasPageCreateCommand(
            owner_id=_OWNER,
            command_id="11111111-1111-4111-8111-111111110079",
            page_id="11111111-1111-4111-8111-111111110080",
            project_id="rollback-atlas-project",
            slug="rollback-atlas-page",
            sources=("synthetic/rollback",),
            content=b"synthetic-opaque-atlas-ciphertext",
            created_at=10,
        ),
    )
    catalog.atlas_pages.edit_page(
        transaction,
        command=AtlasPageEditCommand(
            owner_id=_OWNER,
            command_id="11111111-1111-4111-8111-111111110081",
            page_id="11111111-1111-4111-8111-111111110080",
            expected_revision=1,
            sources=("synthetic/rollback-edit",),
            content=b"synthetic-opaque-atlas-revision-ciphertext",
            updated_at=20,
        ),
    )


def _observe_atlas_rollback(transaction: Transaction) -> tuple[int, int]:
    page = transaction.fetch_one(
        "SELECT COUNT(*) AS value FROM atlas_page WHERE page_id=%s",
        ("11111111-1111-4111-8111-111111110080",),
    )
    revisions = transaction.fetch_one(
        "SELECT COUNT(*) AS value FROM atlas_page_revision WHERE page_id=%s",
        ("11111111-1111-4111-8111-111111110080",),
    )
    assert page is not None and revisions is not None
    return int(page["value"]), int(revisions["value"])


def _write_atlas_links(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    for suffix, slug in ((81, "rollback-link-source"), (82, "rollback-link-target")):
        catalog.atlas_pages.create_page(
            transaction,
            command=AtlasPageCreateCommand(
                owner_id=_OWNER,
                command_id=f"11111111-1111-4111-8111-1111111100{suffix}",
                page_id=f"11111111-1111-4111-8111-1111111100{suffix}",
                project_id="rollback-atlas-link-project",
                slug=slug,
                sources=(),
                content=b"synthetic-opaque-atlas-link-ciphertext",
                created_at=10,
            ),
        )
    catalog.atlas_links.set_links(
        transaction,
        command=AtlasLinkSetCommand(
            owner_id=_OWNER,
            command_id="11111111-1111-4111-8111-111111110083",
            page_id="11111111-1111-4111-8111-111111110081",
            expected_page_revision=1,
            expected_link_revision=0,
            targets=("11111111-1111-4111-8111-111111110082",),
            updated_at=20,
        ),
    )


def _write_completion_wake(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.completion_wake.register_subscription(
        transaction,
        owner_id=_OWNER,
        waiter_operation_id="11111111-1111-4111-8111-111111110077",
        waiter_owner_id=_OWNER,
        source_operation_id="11111111-1111-4111-8111-111111110078",
        source_owner_id=_OWNER,
        terminal_condition="completed",
        source_revision=1,
        current_revision_fence=1,
        created_at=1,
        subscription_id="11111111-1111-4111-8111-111111110076",
    )


def _write_computer_use(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.computer_use.open_session(
        transaction,
        ComputerSession(
            session_id="11111111-1111-4111-8111-111111110090",
            owner_id=_OWNER,
            host_id="rollback-host",
            host_incarnation="rollback-incarnation",
            state="active",
            revision=1,
            created_at=1,
            updated_at=1,
        ),
    )


def _write_capability_publications(
    catalog: api.RepositoryCatalog, transaction: Transaction
) -> None:
    catalog.capability_publications.publish(
        transaction,
        owner_id=_OWNER,
        capability_id="rollback-capability",
        audience_id="rollback-audience",
        capability_revision="rollback-revision",
        metadata_ciphertext="synthetic-rollback-ciphertext",
        metadata_digest=_DIGEST_A,
        published_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
        expected_revision=0,
    )


def _write_notifications(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.notifications.put_preferences(
        transaction,
        owner_id=_OWNER,
        expected_revision=0,
        in_app_completion=True,
        in_app_failure=True,
        in_app_required_action=True,
        inbox_completion=True,
        inbox_failure=True,
        inbox_required_action=True,
        push_completion=False,
        push_failure=False,
        push_required_action=False,
        updated_at=1,
    )
    body = b"opaque-rollback-notification"
    catalog.notifications.create_entry(
        transaction,
        owner_id=_OWNER,
        event_kind="completion",
        source_id="rollback-source",
        deduplication_key="rollback-notification",
        content_format="astral.notification/v1",
        content_ciphertext=body,
        content_sha256=hashlib.sha256(body).hexdigest(),
        created_at=1,
        retention_expires_at=100,
        entry_id="74000000-0000-4000-8000-000000000090",
    )
    catalog.notifications.register_delivery_identity(
        transaction,
        owner_id=_OWNER,
        entry_id="74000000-0000-4000-8000-000000000090",
        delivery_key="rollback-delivery",
        created_at=1,
        delivery_id="74000000-0000-4000-8000-000000000091",
    )


def _observe_notifications(transaction: Transaction) -> object:
    return tuple(
        transaction.fetch_one(f"SELECT COUNT(*) AS value FROM {table}")["value"]
        for table in (
            "notification_preference",
            "notification_inbox_entry",
            "notification_delivery_identity",
        )
    )


def _write_push_deliveries(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    body = b"opaque-rollback-push-payload"
    entry = catalog.notifications.create_entry(
        transaction,
        owner_id=_OWNER,
        event_kind="completion",
        source_id="rollback-push-source",
        deduplication_key="rollback-push-notification",
        content_format="astral.notification/v1",
        content_ciphertext=body,
        content_sha256=hashlib.sha256(body).hexdigest(),
        created_at=1,
        retention_expires_at=100,
        entry_id="74000000-0000-4000-8000-000000000092",
    )
    token = b"opaque-rollback-push-token"
    destination = catalog.push_deliveries.register_destination(
        transaction,
        owner_id=_OWNER,
        destination_key="rollback-push-device",
        token_format="astral.push-token/v1",
        token_ciphertext=token,
        token_sha256=hashlib.sha256(token).hexdigest(),
        created_at=1,
        destination_id="74000000-0000-4000-8000-000000000093",
    )
    catalog.push_deliveries.enqueue_delivery(
        transaction,
        owner_id=_OWNER,
        entry_id=entry.entry_id,
        destination_id=destination.destination_id,
        delivery_key="rollback-push-delivery",
        expected_destination_revision=destination.revision,
        expected_token_version=destination.token_version,
        max_attempts=2,
        available_at=2,
        created_at=2,
        delivery_id="74000000-0000-4000-8000-000000000094",
    )


def _observe_push_deliveries(transaction: Transaction) -> object:
    return tuple(
        transaction.fetch_one(f"SELECT COUNT(*) AS value FROM {table}")["value"]
        for table in ("notification_device_destination", "notification_push_delivery")
    )


def _write_workspaces(catalog: api.RepositoryCatalog, transaction: Transaction) -> None:
    catalog.workspaces.canvas.create(
        transaction,
        CanvasComponentRecord(
            row_id="rollback-workspace-row",
            conversation_id=_SUPPORT_CHAT,
            owner_id=_OWNER,
            component_id="rollback-workspace-component",
            payload={"type": "Text", "text": "rollback workspace"},
            component_type="Text",
            title="Rollback workspace",
            position=1,
            created_at=11,
            updated_at=11,
        ),
    )


def _observe_work_admission_revision(transaction: Transaction) -> object:
    row = transaction.fetch_one(
        "SELECT config_revision FROM operation_admission_class WHERE class_name = %s",
        (AdmissionClass.GLOBAL.value,),
    )
    assert row is not None
    return str(row["config_revision"])


ROLLBACK_CASES = (
    RollbackCase(
        "assignments",
        _write_assignments,
        _count("persistent_assignment", "id", "79000000-0000-4000-8000-000000000002"),
        1,
        0,
    ),
    RollbackCase(
        "agents",
        _write_agents,
        _count("user_agent", "agent_id", "rollback-agent"),
        1,
        0,
    ),
    RollbackCase(
        "artifacts",
        _write_artifacts,
        _count("user_attachments", "attachment_id", "rollback-attachment"),
        1,
        0,
    ),
    RollbackCase(
        "attachment_parsers",
        _write_attachment_parsers,
        _count("attachment_parser", "gap_fingerprint", "rollback-parser-gap"),
        1,
        0,
    ),
    RollbackCase(
        "audit",
        _write_audit,
        _count("audit_events", "event_id", _AUDIT_EVENT_ID),
        1,
        0,
    ),
    RollbackCase(
        "audit_retention",
        _write_audit_retention,
        _observe_audit_retention,
        (1, 0),
        (0, 1),
        _prepare_audit_retention,
    ),
    RollbackCase(
        "authority",
        _write_authority,
        _count("astralplane_authority_binding", "binding_id", "rollback-authority-binding"),
        1,
        0,
    ),
    RollbackCase(
        "background_tasks",
        _write_background_tasks,
        _count("background_task", "task_id", "rollback-background-task"),
        1,
        0,
    ),
    RollbackCase(
        "chat_queue",
        _write_chat_queue,
        _count("chat_queue_item", "item_id", _CHAT_QUEUE_ITEM),
        1,
        0,
    ),
    RollbackCase(
        "chat_read_receipts",
        _write_chat_read_receipts,
        _count("chat_read_receipt", "owner_id", _OWNER),
        1,
        0,
    ),
    RollbackCase(
        "chat_steps", _write_chat_steps, _count("chat_steps", "id", "rollback-chat-step"), 1, 0
    ),
    RollbackCase(
        "derived_chat",
        _write_derived_chat,
        _count("chats", "event_id", _mesh_reference(b"rollback-derived-chat")),
        1,
        0,
    ),
    RollbackCase(
        "event_log",
        _write_event_log,
        _count("event_entry", "owner_id", _OWNER),
        1,
        0,
    ),
    RollbackCase(
        "event_roster",
        _write_event_roster,
        _count("event_roster_entry", "owner_id", _OWNER),
        1,
        0,
    ),
    RollbackCase(
        "learning_ledger",
        _write_learning_ledger,
        _count("learning_ledger_entry", "owner_id", _OWNER),
        1,
        0,
    ),
    RollbackCase(
        "runs",
        _write_runs,
        _count("run_lease_entry", "owner_id", _OWNER),
        1,
        0,
    ),
    RollbackCase(
        "conversation_files",
        _write_conversation_files,
        _count("chat_files", "backend_path", "uploads/rollback.txt"),
        1,
        0,
    ),
    RollbackCase(
        "credentials",
        _write_credentials,
        _count("user_credentials", "credential_key", "rollback"),
        1,
        0,
    ),
    RollbackCase(
        "draft_agents", _write_draft_agents, _count("draft_agents", "id", "rollback-draft"), 1, 0
    ),
    RollbackCase(
        "generated_agent_publications",
        _write_generated_agent_publications,
        _count("draft_artifact_publication", "publication_id", _GENERATED_PUBLICATION_ID),
        1,
        0,
    ),
    RollbackCase(
        "encrypted_llm_config",
        _write_encrypted_llm_config,
        _count("user_llm_config", "user_id", _OWNER),
        1,
        0,
    ),
    RollbackCase(
        "encrypted_typesafe_credential",
        _write_encrypted_typesafe_credential,
        _count("user_typesafe_credential", "user_id", _OWNER),
        1,
        0,
    ),
    RollbackCase(
        "framework_credentials",
        _write_framework_credentials,
        _count("framework_credential", "id", "5b8e0000-0000-4000-8000-0000000f8acc"),
        1,
        0,
        prepare=_prepare_framework_credentials,
    ),
    RollbackCase("history", _write_history, _count("chats", "id", "rollback-history-chat"), 1, 0),
    RollbackCase(
        "harness_cleanup",
        _write_harness_cleanup,
        _count("draft_agents", "id", "__verif__rollback_cleanup_draft"),
        0,
        1,
        _prepare_harness_cleanup,
    ),
    RollbackCase("identity", _write_identity, _count("users", "id", "rollback-new-owner"), 1, 0),
    RollbackCase(
        "knowledge", _write_knowledge, _count("interaction_log", "tool_name", "rollback.tool"), 1, 0
    ),
    RollbackCase(
        "maintenance",
        _write_maintenance,
        _count("maintenance_unit", "unit_id", "11111111-1111-4111-8111-111111110074"),
        1,
        0,
    ),
    RollbackCase(
        "mesh_enrollment",
        _write_mesh_enrollment,
        _count("mesh_record", "mesh_id", "11111111-1111-4111-8111-111111110075"),
        1,
        0,
    ),
    RollbackCase(
        "verification_credentials",
        _write_verification_credentials,
        _observe_verification_credentials,
        (1, 1),
        (0, 0),
    ),
    RollbackCase(
        "notifications",
        _write_notifications,
        _observe_notifications,
        (1, 1, 1),
        (0, 0, 0),
    ),
    RollbackCase(
        "push_deliveries",
        _write_push_deliveries,
        _observe_push_deliveries,
        (1, 1),
        (0, 0),
    ),
    RollbackCase("projects", _write_projects, _observe_projects, (1, 1), (0, 0)),
    RollbackCase(
        "observations",
        _write_observations,
        _observe_observations,
        (1, 1),
        (0, 0),
    ),
    RollbackCase(
        "offline_grants",
        _write_offline_grants,
        _count("user_offline_grant", "id", "9ef050be-0d5f-4a82-b3cb-410de6d9074a"),
        1,
        0,
    ),
    RollbackCase(
        "outbox",
        _write_outbox,
        _count("astralplane_outbox", "entry_id", "rollback-outbox-entry"),
        1,
        0,
    ),
    RollbackCase(
        "preferences",
        _write_preferences,
        _count("component_feedback", "id", "74000000-0000-4000-8000-000000000010"),
        1,
        0,
    ),
    RollbackCase(
        "personalization_graph",
        _write_personalization_graph,
        _count("short_term_signal", "id", "74000000-0000-4000-8000-000000000011"),
        1,
        0,
    ),
    RollbackCase(
        "purge",
        _write_purge,
        _count(
            "astralplane_purge_tombstone",
            "object_id",
            "rollback-purge-object",
            extra_sql=" AND owner_id = %s AND target_scope = 'attachment_prefix'",
            extra_parameters=(_OWNER,),
        ),
        1,
        0,
        _prepare_purge,
    ),
    RollbackCase(
        "quality_audit",
        _write_quality_audit,
        _count("test_runs", "id", "rollback-quality-run"),
        1,
        0,
    ),
    RollbackCase(
        "remote", _write_remote, _count("remote_machine", "machine_id", "rollback-machine"), 1, 0
    ),
    RollbackCase(
        "remote_operation_proposals",
        _write_remote_operation_proposals,
        _count("remote_operation_proposal", "proposal_id", "rollback-remote-proposal"),
        1,
        0,
    ),
    RollbackCase(
        "revocations",
        _write_revocations,
        _count(
            "auth_revocation_queue",
            "user_id",
            _OWNER,
            extra_sql=" AND refresh_token_enc = %s",
            extra_parameters=("opaque-rollback-refresh-token",),
        ),
        1,
        0,
    ),
    RollbackCase(
        "saved_components",
        _write_saved_components,
        _count("saved_components", "id", "rollback-saved-row"),
        1,
        0,
    ),
    RollbackCase(
        "scheduler",
        _write_scheduler,
        _count("scheduled_job", "id", "33333333-3333-4333-8333-333333330074"),
        1,
        0,
    ),
    RollbackCase(
        "share_grants", _write_share_grants, _count("share_grant", "token_sha256", _DIGEST_B), 1, 0
    ),
    RollbackCase(
        "stop_epochs", _write_stop_epochs, _count("owner_stop_epoch", "owner_id", _OWNER), 1, 0
    ),
    RollbackCase(
        "tool_policy_state",
        _write_tool_policy_state,
        _count("user_preferences", "user_id", _OWNER),
        1,
        0,
    ),
    RollbackCase(
        "tracked_jobs",
        _write_tracked_jobs,
        _count("tracked_job", "tracked_job_id", "rollback-tracked-job"),
        1,
        0,
    ),
    RollbackCase(
        "tutorials", _write_tutorials, _count("tutorial_step", "slug", "rollback-tutorial"), 1, 0
    ),
    RollbackCase(
        "voice",
        _write_voice,
        _count("voice_session", "session_id", "74000000-0000-4000-8000-000000000012"),
        1,
        0,
    ),
    RollbackCase(
        "work_admission",
        _write_work_admission,
        _observe_work_admission_revision,
        "rollback-contract-v1",
        "060-defaults",
    ),
    RollbackCase(
        "workspaces",
        _write_workspaces,
        _count("saved_components", "id", "rollback-workspace-row"),
        1,
        0,
    ),
    RollbackCase(
        "completion_wake",
        _write_completion_wake,
        _count(
            "completion_subscription",
            "subscription_id",
            "11111111-1111-4111-8111-111111110076",
        ),
        1,
        0,
    ),
    RollbackCase(
        "computer_use",
        _write_computer_use,
        _count(
            "computer_session",
            "session_id",
            "11111111-1111-4111-8111-111111110090",
            extra_sql=" AND owner_id = %s",
            extra_parameters=(_OWNER,),
        ),
        1,
        0,
    ),
    RollbackCase(
        "capability_publications",
        _write_capability_publications,
        _count(
            "capability_publication",
            "capability_id",
            "rollback-capability",
            extra_sql=" AND owner_id = %s AND audience_id = %s",
            extra_parameters=(_OWNER, "rollback-audience"),
        ),
        1,
        0,
    ),
    RollbackCase(
        "atlas_pages",
        _write_atlas_pages,
        _observe_atlas_rollback,
        (1, 2),
        (0, 0),
    ),
    RollbackCase(
        "atlas_links",
        _write_atlas_links,
        _count(
            "atlas_page_link",
            "source_page_id",
            "11111111-1111-4111-8111-111111110081",
        ),
        1,
        0,
    ),
)


def test_rollback_matrix_exactly_classifies_every_public_catalog_member() -> None:
    public_keys = tuple(api.create_repository_catalog().as_mapping())
    applicable_keys = tuple(case.key for case in ROLLBACK_CASES)

    assert len(applicable_keys) == len(set(applicable_keys)) == 58
    assert tuple(key for key in public_keys if key != "agent_management") == applicable_keys
    read_only_methods = tuple(
        name
        for name in dir(api.create_agent_management_repository())
        if not name.startswith("_")
        and callable(getattr(api.create_agent_management_repository(), name))
    )
    assert read_only_methods == ("get_detail_context", "get_list_context")


@pytest.mark.parametrize("case", ROLLBACK_CASES, ids=lambda case: case.key)
def test_successful_repository_write_is_removed_by_caller_rollback(
    catalog_database: _CatalogDatabase,
    case: RollbackCase,
) -> None:
    if case.prepare is not None:
        case.prepare(catalog_database)

    with (
        pytest.raises(_ForcedCallerRollbackError, match=case.key),
        catalog_database.database.transaction() as transaction,
    ):
        case.write(catalog_database.catalog, transaction)
        assert case.observe(transaction) == case.during
        raise _ForcedCallerRollbackError(case.key)

    with catalog_database.database.transaction() as transaction:
        assert case.observe(transaction) == case.after
