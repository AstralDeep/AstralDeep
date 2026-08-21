from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from astralplane import (
    GENERATED_AGENT_BUNDLE_CONTRACT,
    BundleRecoveryResult,
    FinalizedBundle,
    GeneratedAgentPublicationResultMetadata,
    ImmutableBundleStore,
    PublishedBundle,
    StagedBundleReceipt,
    canonical_bundle_digest,
)

from orchestrator.generated_agent_publication import (
    GeneratedAgentPublicationRecoveryPendingError,
    GeneratedAgentPublicationRequest,
    GeneratedAgentPublicationService,
    generated_agent_publication_identity,
)
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    OperationState,
    WorkAdmissionCoordinator,
)
from tests.helpers.voice_plane_runtime import (
    PlaneTestRuntime,
    isolated_plane_runtime,
    plane_work_admission_repository,
)


_LOCK_DIGEST = "b" * 64
_FILES = {
    "agent_main.py": "def run():\n    return 'live-postgres'\n",
    "astralprims_ui.py": "UI = {'type': 'text', 'content': 'ready'}\n",
    "protected_executor.py": "EXECUTION_POLICY = 'protected'\n",
    "mcp_tools.py": "TOOLS = ()\n",
}
_RESULT_METADATA = GeneratedAgentPublicationResultMetadata(
    security_report='{"safe":true}',
    validation_report='{"valid":true}',
    required_credentials='["service-token"]',
)


@pytest.fixture(scope="module")
def postgres_runtime() -> Iterator[PlaneTestRuntime]:
    with isolated_plane_runtime("generated_pub_service_074") as runtime:
        yield runtime


class _ObservedBundleStore(ImmutableBundleStore):
    def __init__(
        self, root: Path, *, crash_after_first_promotion: bool = False
    ) -> None:
        super().__init__(root, contract=GENERATED_AGENT_BUNDLE_CONTRACT)
        self.stage_calls = 0
        self.promote_calls = 0
        self.recover_calls = 0
        self._crash_after_first_promotion = crash_after_first_promotion

    def stage(
        self,
        finalized: FinalizedBundle,
        **kwargs: Any,
    ) -> StagedBundleReceipt:
        self.stage_calls += 1
        return super().stage(finalized, **kwargs)

    def promote_staged(
        self,
        receipt: StagedBundleReceipt,
        **kwargs: Any,
    ) -> PublishedBundle:
        self.promote_calls += 1
        published = super().promote_staged(receipt, **kwargs)
        if self._crash_after_first_promotion:
            self._crash_after_first_promotion = False
            raise RuntimeError("simulated lost acknowledgement after durable promotion")
        return published

    def recover(self, **kwargs: Any) -> BundleRecoveryResult:
        self.recover_calls += 1
        return super().recover(**kwargs)


def _admission(runtime: PlaneTestRuntime) -> WorkAdmissionCoordinator:
    classes = (
        AdmissionClassConfig(
            class_name=AdmissionClass.GLOBAL,
            parent_class_name=None,
            active_limit=8,
            queue_limit=0,
            max_wait_ms=0,
            config_revision="generated-publication-postgres-074",
        ),
        AdmissionClassConfig(
            class_name=AdmissionClass.SYSTEM,
            parent_class_name=AdmissionClass.GLOBAL,
            active_limit=4,
            queue_limit=8,
            max_wait_ms=5_000,
            config_revision="generated-publication-postgres-074",
        ),
    )
    return WorkAdmissionCoordinator(
        admission_classes=classes,
        repository=plane_work_admission_repository(runtime),
        operation_retention=timedelta(hours=24),
    )


def _bundle(*, agent_id: str, revision_id: str) -> FinalizedBundle:
    digest = canonical_bundle_digest(_FILES, GENERATED_AGENT_BUNDLE_CONTRACT)
    manifest: dict[str, Any] = {
        "agent_name": "Generated PostgreSQL Test Agent",
        "agent_id": agent_id,
        "bundle_sha256": digest,
        "constitution_version": "0.1.0",
        "description": "live PostgreSQL publication service fixture",
        "digest_algorithm": "sha256",
        "required_runtime_lock_sha256": _LOCK_DIGEST,
        "revision_id": revision_id,
        "runtime_contract_version": 3,
        "files": [
            {
                "name": name,
                "sha256": hashlib.sha256(_FILES[name].encode()).hexdigest(),
                "size_bytes": len(_FILES[name].encode()),
            }
            for name in GENERATED_AGENT_BUNDLE_CONTRACT.file_names
        ],
        "manifest_version": 2,
    }
    manifest_json = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    return FinalizedBundle(
        contract=GENERATED_AGENT_BUNDLE_CONTRACT,
        files=_FILES,
        bundle_sha256=digest,
        manifest=manifest,
        manifest_json=manifest_json,
    )


def _claimed_request(
    runtime: PlaneTestRuntime,
) -> GeneratedAgentPublicationRequest:
    owner_id = f"publication-owner-{uuid.uuid4().hex}"
    draft_uuid = str(uuid.uuid4())
    claim_id = str(uuid.uuid4())
    target_agent_id = f"generated-agent-{uuid.uuid4().hex}"
    with runtime.transaction() as transaction:
        draft = runtime.repositories.draft_agents.create_draft(
            transaction,
            draft_id=draft_uuid,
            owner_id=owner_id,
            agent_name="Generated PostgreSQL Test Agent",
            agent_slug=target_agent_id,
            description="live PostgreSQL publication service fixture",
            observed_at=1_720_000_000_000,
            draft_uuid=draft_uuid,
            target_agent_id=target_agent_id,
        )
        runtime.repositories.agents.create_agent(
            transaction,
            agent_id=target_agent_id,
            owner_id=owner_id,
            display_name="Generated PostgreSQL Test Agent",
            observed_at=1_720_000_000_000,
            draft_id=draft.draft_id,
        )
        claimed = runtime.repositories.draft_agents.claim_generation(
            transaction,
            owner_id=owner_id,
            draft_id=draft.draft_id,
            expected_revision=draft.state_revision,
            claim_id=claim_id,
            lease_seconds=300,
        )
    identity = generated_agent_publication_identity(
        owner_id=owner_id,
        draft_uuid=draft_uuid,
        source_state_revision=claimed.state_revision,
        generation_claim_id=claim_id,
        target_agent_id=target_agent_id,
    )
    return GeneratedAgentPublicationRequest(
        owner_id=owner_id,
        draft_uuid=draft_uuid,
        source_state_revision=claimed.state_revision,
        generation_claim_id=claim_id,
        target_agent_id=target_agent_id,
        bundle=_bundle(
            agent_id=target_agent_id,
            revision_id=str(identity.target_revision_id),
        ),
        runtime_contract_version=3,
        release_lock_digest=_LOCK_DIGEST,
        generation_result=_RESULT_METADATA,
    )


def _service(
    runtime: PlaneTestRuntime,
    store: _ObservedBundleStore,
) -> tuple[GeneratedAgentPublicationService, WorkAdmissionCoordinator]:
    admission = _admission(runtime)
    service = GeneratedAgentPublicationService(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
        bundle_store=store,
        work_admission=admission,
        heartbeat_interval_seconds=5,
        recovery_interval_seconds=60,
    )
    return service, admission


def _revision_directory(
    store: _ObservedBundleStore,
    revision_relative_path: str,
) -> Path:
    return store.root.joinpath(*PurePosixPath(revision_relative_path).parts)


@pytest.mark.asyncio
async def test_service_publishes_and_replays_without_second_filesystem_publication(
    postgres_runtime: PlaneTestRuntime,
    tmp_path: Path,
) -> None:
    request = _claimed_request(postgres_runtime)
    store = _ObservedBundleStore(tmp_path / "published")
    service, admission = _service(postgres_runtime, store)
    try:
        published = await service.publish(request)
        revision_directory = _revision_directory(
            store, published.publication.revision_relative_path
        )

        assert published.publication.state == "published"
        assert published.publication.state_revision == 3
        assert published.published.bundle_sha256 == request.bundle.bundle_sha256
        assert published.published.manifest_json == request.bundle.manifest_json
        assert published.generation_result == _RESULT_METADATA
        assert revision_directory.is_dir()
        assert not (store.root / published.publication.staging_relative_path).exists()
        assert store.stage_calls == 1
        assert store.promote_calls == 1
        assert store.recover_calls == 0

        replayed = await service.publish(request)

        assert replayed.publication == published.publication
        assert replayed.revision == published.revision
        assert replayed.published.manifest_json == published.published.manifest_json
        assert replayed.generation_result == published.generation_result
        assert store.stage_calls == 1
        assert store.promote_calls == 1
        assert store.recover_calls == 0

        with postgres_runtime.transaction() as transaction:
            draft = postgres_runtime.repositories.draft_agents.get_draft_by_uuid(
                transaction,
                owner_id=request.owner_id,
                draft_uuid=request.draft_uuid,
            )
        assert draft is not None
        assert draft.status == "generated"
        assert draft.generation_claim_id is None
        assert draft.published_revision_id == published.revision.revision_id
        assert draft.security_report == _RESULT_METADATA.security_report
        assert draft.validation_report == _RESULT_METADATA.validation_report
        assert draft.required_credentials == _RESULT_METADATA.required_credentials

        operation = admission.repository.get_operation_for_administration(
            uuid.UUID(published.publication.operation_id or "")
        )
        assert operation is not None
        assert operation.state is OperationState.COMPLETED
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_service_recovers_promoted_bytes_after_lost_commit_boundary(
    postgres_runtime: PlaneTestRuntime,
    tmp_path: Path,
) -> None:
    request = _claimed_request(postgres_runtime)
    store = _ObservedBundleStore(
        tmp_path / "recovered",
        crash_after_first_promotion=True,
    )
    service, admission = _service(postgres_runtime, store)
    try:
        with pytest.raises(GeneratedAgentPublicationRecoveryPendingError):
            await service.publish(request)

        with postgres_runtime.transaction() as transaction:
            pending = postgres_runtime.repositories.generated_agent_publications.get_by_source(
                transaction,
                owner_id=request.owner_id,
                draft_uuid=request.draft_uuid,
                source_state_revision=request.source_state_revision,
            )
            pending_draft = (
                postgres_runtime.repositories.draft_agents.get_draft_by_uuid(
                    transaction,
                    owner_id=request.owner_id,
                    draft_uuid=request.draft_uuid,
                )
            )
        assert pending is not None
        assert pending.state == "validated"
        assert pending.state_revision == 2
        assert pending_draft is not None
        assert pending_draft.status == "generating"
        assert pending_draft.security_report == _RESULT_METADATA.security_report
        assert pending_draft.validation_report == _RESULT_METADATA.validation_report
        assert (
            pending_draft.required_credentials == _RESULT_METADATA.required_credentials
        )
        assert _revision_directory(store, pending.revision_relative_path).is_dir()
        assert store.stage_calls == 1
        assert store.promote_calls == 1

        original_operation = admission.repository.get_operation_for_administration(
            uuid.UUID(pending.operation_id or "")
        )
        assert original_operation is not None
        assert original_operation.state is OperationState.RETRYABLE

        report = await service.recover_once()

        assert report.inspected == 1
        assert report.recovered == 1
        assert report.failed == 0
        assert report.skipped_live == 0
        assert report.degraded_publication_ids == ()
        assert store.recover_calls == 1
        assert store.stage_calls == 1
        assert store.promote_calls == 1

        recovered = await service.load_published(
            owner_id=request.owner_id,
            draft_uuid=request.draft_uuid,
            source_state_revision=request.source_state_revision,
        )
        assert recovered is not None
        assert recovered.publication.state == "published"
        assert recovered.publication.state_revision == 4
        assert recovered.published.bundle_sha256 == request.bundle.bundle_sha256
        assert recovered.generation_result == _RESULT_METADATA
        assert recovered.publication.operation_id != str(
            original_operation.operation_id
        )

        recovery_operation = admission.repository.get_operation_for_administration(
            uuid.UUID(recovered.publication.operation_id or "")
        )
        assert recovery_operation is not None
        assert recovery_operation.state is OperationState.COMPLETED
        assert recovery_operation.parent_operation_id == original_operation.operation_id
    finally:
        await service.close()
