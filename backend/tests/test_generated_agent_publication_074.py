from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import uuid
from contextlib import contextmanager
from dataclasses import astuple, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from astralplane import (
    GENERATED_AGENT_BUNDLE_CONTRACT,
    AgentRevisionRecord,
    BundlePublicationKey,
    BundlePublicationReceipt,
    BundleRecoveryDisposition,
    BundleRecoveryResult,
    DraftPublicationRecord,
    FinalizedBundle,
    GeneratedAgentPublicationIntent,
    GeneratedAgentPublicationResultMetadata,
    PublishedBundle,
    StagedBundleReceipt,
    canonical_bundle_digest,
    generated_agent_publication_operation_binding,
    generated_agent_publication_paths,
    paths_for,
)

from orchestrator.generated_agent_publication import (
    GenerationClaimHeartbeat,
    GenerationClaimLostError,
    GeneratedAgentPublicationManagedCancellation,
    GeneratedAgentPublicationManagedError,
    GeneratedAgentPublicationPreIntentError,
    GeneratedAgentPublicationRecoveryPendingError,
    GeneratedAgentPublicationRequest,
    GeneratedAgentPublicationService,
    generated_agent_publication_identity,
)
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    InMemoryWorkAdmissionRepository,
    OperationState,
    OwnerScope,
    WorkAdmissionCoordinator,
)


OWNER = "owner-generated-publication"
DRAFT = "10000000-0000-4000-8000-000000000074"
CLAIM = "20000000-0000-4000-8000-000000000074"
AGENT = "owner-generated-agent"
LOCK_DIGEST = "b" * 64
FILES = {
    "agent_main.py": "main\n",
    "astralprims_ui.py": "ui\n",
    "protected_executor.py": "executor\n",
    "mcp_tools.py": "tools\n",
}
RESULT_METADATA = GeneratedAgentPublicationResultMetadata(
    security_report='{"safe":true}',
    validation_report='{"valid":true}',
    required_credentials='["service-token"]',
)


def _identity(**changes: object):
    values: dict[str, object] = {
        "owner_id": OWNER,
        "draft_uuid": DRAFT,
        "source_state_revision": 7,
        "generation_claim_id": CLAIM,
        "target_agent_id": AGENT,
    }
    values.update(changes)
    return generated_agent_publication_identity(**values)  # type: ignore[arg-type]


def _bundle(
    revision_id: str,
    *,
    files: dict[str, str] | None = None,
) -> FinalizedBundle:
    bundle_files = FILES if files is None else files
    digest = canonical_bundle_digest(bundle_files, GENERATED_AGENT_BUNDLE_CONTRACT)
    manifest: dict[str, Any] = {
        "agent_name": "Generated Test Agent",
        "agent_id": AGENT,
        "bundle_sha256": digest,
        "constitution_version": "0.1.0",
        "description": "publication service fixture",
        "digest_algorithm": "sha256",
        "required_runtime_lock_sha256": LOCK_DIGEST,
        "revision_id": revision_id,
        "runtime_contract_version": 3,
        "files": [
            {
                "name": name,
                "sha256": hashlib.sha256(bundle_files[name].encode()).hexdigest(),
                "size_bytes": len(bundle_files[name].encode()),
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
        files=bundle_files,
        bundle_sha256=digest,
        manifest=manifest,
        manifest_json=manifest_json,
    )


def _request(**changes: object) -> GeneratedAgentPublicationRequest:
    identity = _identity()
    values: dict[str, object] = {
        "owner_id": OWNER,
        "draft_uuid": DRAFT,
        "source_state_revision": 7,
        "generation_claim_id": CLAIM,
        "target_agent_id": AGENT,
        "bundle": _bundle(str(identity.target_revision_id)),
        "runtime_contract_version": 3,
        "release_lock_digest": LOCK_DIGEST,
        "generation_result": RESULT_METADATA,
    }
    values.update(changes)
    return GeneratedAgentPublicationRequest(**values)  # type: ignore[arg-type]


def _binding(request: GeneratedAgentPublicationRequest) -> Any:
    identity = generated_agent_publication_identity(
        owner_id=request.owner_id,
        draft_uuid=request.draft_uuid,
        source_state_revision=request.source_state_revision,
        generation_claim_id=request.generation_claim_id,
        target_agent_id=request.target_agent_id,
    )
    return generated_agent_publication_operation_binding(
        owner_id=request.owner_id,
        publication_id=str(identity.publication_id),
        draft_uuid=request.draft_uuid,
        source_state_revision=request.source_state_revision,
        generation_claim_id=request.generation_claim_id,
        target_agent_id=request.target_agent_id,
        target_revision_id=str(identity.target_revision_id),
        bundle=request.bundle,
        runtime_contract_version=request.runtime_contract_version,
        release_lock_digest=request.release_lock_digest,
        promotion_token=str(identity.promotion_token),
        compatibility_state=request.compatibility_state,
    )


def _coordinator(
    *,
    clock: Any | None = None,
    slot_lease: timedelta = timedelta(seconds=30),
) -> WorkAdmissionCoordinator:
    repository = InMemoryWorkAdmissionRepository()
    return WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.GLOBAL,
                parent_class_name=None,
                active_limit=8,
                queue_limit=16,
                max_wait_ms=10_000,
                config_revision="generated-publication-test",
            ),
            AdmissionClassConfig(
                class_name=AdmissionClass.SYSTEM,
                parent_class_name=AdmissionClass.GLOBAL,
                active_limit=4,
                queue_limit=8,
                max_wait_ms=10_000,
                config_revision="generated-publication-test",
            ),
        ),
        repository=repository,
        clock=clock or (lambda: datetime.now(UTC)),
        slot_lease=slot_lease,
    )


class _FakeRuntime:
    def __init__(self) -> None:
        self.threads: list[int] = []
        self.transactions: list[object] = []

    @contextmanager
    def transaction(self):
        self.threads.append(threading.get_ident())
        transaction = object()
        self.transactions.append(transaction)
        yield transaction


class _FakeJournal:
    def __init__(self, *, begin_error: BaseException | None = None) -> None:
        self.begin_error = begin_error
        self.begin_after_state_error: BaseException | None = None
        self.publication: DraftPublicationRecord | None = None
        self.revision: AgentRevisionRecord | None = None
        self.draft = SimpleNamespace(
            error_message=None,
            security_report=None,
            validation_report=None,
            required_credentials=None,
            status="generating",
            generation_claim_id=CLAIM,
            published_revision_id=None,
        )
        self.agent = SimpleNamespace(
            agent_id=AGENT,
            owner_id=OWNER,
            deleted_at=None,
        )
        self.expired_claims: tuple[Any, ...] = ()
        self.reclaimed_expired_claim: Any | None = None
        self.finished_expired_claim: Any | None = None
        self.expired_finish_entered = threading.Event()
        self.expired_finish_release = threading.Event()
        self.expired_finish_release.set()
        self.events: list[str] = []
        self.terminal_transactions: list[object] = []
        self.renewals = 0
        self.renew_error: BaseException | None = None
        self.commit_error: BaseException | None = None
        self.commit_after_state_error: BaseException | None = None
        self.commit_entered = threading.Event()
        self.commit_release = threading.Event()
        self.commit_release.set()
        self.fail_error: BaseException | None = None
        self.fail_after_state_error: BaseException | None = None
        self.rebind_error: BaseException | None = None
        self.begin_entered = threading.Event()
        self.begin_release = threading.Event()
        self.begin_release.set()
        self.get_entered = threading.Event()
        self.get_release = threading.Event()
        self.get_release.set()
        self.mark_staged_entered = threading.Event()
        self.mark_staged_release = threading.Event()
        self.mark_staged_release.set()
        self.fail_entered = threading.Event()
        self.fail_release = threading.Event()
        self.fail_release.set()
        self.rebind_entered = threading.Event()
        self.rebind_release = threading.Event()
        self.rebind_release.set()
        self.missing_revision = False
        self.missing_draft = False
        self.get_error: BaseException | None = None

    def begin_intent(self, _transaction: object, **values: Any):
        self.events.append("begin")
        if self.begin_error is not None:
            raise self.begin_error
        now = datetime.now(UTC)
        self.publication = DraftPublicationRecord(
            publication_id=values["publication_id"],
            draft_uuid=values["draft_uuid"],
            owner_id=values["owner_id"],
            source_state_revision=values["source_state_revision"],
            generation_claim_id=values["generation_claim_id"],
            target_agent_id=values["target_agent_id"],
            target_revision_id=values["target_revision_id"],
            operation_id=str(values["attempt"].operation_id),
            operation_execution_generation=values["attempt"].execution_generation,
            staging_relative_path=values["staging_relative_path"],
            revision_relative_path=values["revision_relative_path"],
            artifact_digest=None,
            manifest_digest=None,
            state="claimed",
            state_revision=0,
            created_at=now,
            published_at=None,
            failed_at=None,
            failure_code=None,
        )
        bundle = values["bundle"]
        self.revision = AgentRevisionRecord(
            revision_id=values["target_revision_id"],
            agent_id=values["target_agent_id"],
            owner_id=values["owner_id"],
            revision_number=0,
            parent_revision_id=None,
            previous_good_revision_id=None,
            artifact_digest=bundle.bundle_sha256,
            manifest=bundle.manifest,
            artifact_relative_path=values["revision_relative_path"],
            runtime_contract_version=values["runtime_contract_version"],
            release_lock_digest=values["release_lock_digest"],
            compatibility_state=values["compatibility_state"],
            state="prepared",
            promotion_token=values["promotion_token"],
            state_revision=0,
            created_at=now,
            confirmed_at=None,
            promoted_at=None,
            failed_at=None,
            failure_code=None,
        )
        self.begin_entered.set()
        self.begin_release.wait(2)
        if self.begin_after_state_error is not None:
            raise self.begin_after_state_error
        return GeneratedAgentPublicationIntent(self.publication, self.revision, False)

    def get_by_source(self, _transaction: object, **values: Any):
        self.get_entered.set()
        self.get_release.wait(2)
        if self.get_error is not None:
            raise self.get_error
        if self.publication is not None and (
            values.get("owner_id") != self.publication.owner_id
            or values.get("draft_uuid") != self.publication.draft_uuid
            or values.get("source_state_revision")
            != self.publication.source_state_revision
        ):
            return None
        return self.publication

    def list_reconcilable_for_administration(
        self,
        _transaction: object,
        *,
        limit: int,
        after_created_at: datetime | None = None,
        after_publication_id: str | None = None,
    ):
        if self.publication is None or self.publication.state in {
            "published",
            "failed",
        }:
            return ()
        if after_created_at is not None and (
            self.publication.created_at,
            self.publication.publication_id,
        ) <= (after_created_at, after_publication_id):
            return ()
        return (self.publication,)[:limit]

    def assert_current_attempt(self, _transaction: object, **values: Any):
        assert values["expected"] == self.publication
        self.events.append("fence")
        return self.publication

    def renew_generation_claim(self, _transaction: object, **values: Any):
        assert values["expected"] == self.publication
        self.renewals += 1
        if self.renew_error is not None:
            raise self.renew_error
        return self.draft

    def mark_staged(self, _transaction: object, **values: Any):
        self.events.append("mark_staged")
        if values["expected"] != self.publication:
            raise RuntimeError("stale staged transition")
        self.mark_staged_entered.set()
        self.mark_staged_release.wait(2)
        self.publication = replace(values["expected"], state="staged", state_revision=1)
        return self.publication

    def mark_validated(self, _transaction: object, **values: Any):
        self.events.append("mark_validated")
        if values["expected"] != self.publication:
            raise RuntimeError("stale validated transition")
        result = values["generation_result"]
        for name in (
            "error_message",
            "security_report",
            "validation_report",
            "required_credentials",
        ):
            setattr(self.draft, name, getattr(result, name))
        self.publication = replace(
            values["expected"],
            state="validated",
            state_revision=2,
            artifact_digest=values["artifact_digest"],
            manifest_digest=values["manifest_digest"],
        )
        return self.publication

    def commit_published(self, transaction: object, **values: Any):
        self.events.append("commit")
        self.terminal_transactions.append(transaction)
        if values["expected"] != self.publication:
            raise RuntimeError("stale publication commit")
        if self.commit_error is not None:
            raise self.commit_error
        self.publication = replace(
            values["expected"],
            state="published",
            state_revision=values["expected"].state_revision + 1,
            published_at=datetime.now(UTC),
        )
        self.draft.status = "generated"
        self.draft.generation_claim_id = None
        self.draft.published_revision_id = self.publication.target_revision_id
        self.commit_entered.set()
        self.commit_release.wait(2)
        if self.commit_after_state_error is not None:
            raise self.commit_after_state_error
        return self.publication

    def fail(self, transaction: object, **values: Any):
        self.events.append("fail")
        self.terminal_transactions.append(transaction)
        if values["expected"] != self.publication:
            raise RuntimeError("stale publication failure")
        self.fail_entered.set()
        self.fail_release.wait(2)
        if self.fail_error is not None:
            raise self.fail_error
        self.publication = replace(
            values["expected"],
            state="failed",
            state_revision=values["expected"].state_revision + 1,
            failed_at=datetime.now(UTC),
            failure_code=values["failure_code"],
        )
        self.draft.status = "error"
        self.draft.generation_claim_id = None
        self.draft.error_message = values["safe_error_message"]
        if self.fail_after_state_error is not None:
            raise self.fail_after_state_error
        return self.publication

    def rebind_recovery_attempt(self, _transaction: object, **values: Any):
        if self.rebind_error is not None:
            raise self.rebind_error
        expected = values["expected"]
        attempt = values["new_attempt"]
        self.publication = replace(
            expected,
            operation_id=str(attempt.operation_id),
            operation_execution_generation=attempt.execution_generation,
            state_revision=expected.state_revision + 1,
        )
        self.events.append("rebind")
        self.rebind_entered.set()
        self.rebind_release.wait(2)
        return self.publication


class _FakeAgents:
    def __init__(self, journal: _FakeJournal) -> None:
        self.journal = journal

    def get_revision(self, _transaction: object, **_values: Any):
        if self.journal.missing_revision:
            return None
        return self.journal.revision

    def get_agent(self, _transaction: object, **_values: Any):
        return self.journal.agent


class _FakeDrafts:
    def __init__(self, journal: _FakeJournal) -> None:
        self.journal = journal

    def get_draft_by_uuid(self, _transaction: object, **_values: Any):
        if self.journal.missing_draft:
            return None
        return self.journal.draft

    def list_expired_generation_claims_for_administration(
        self,
        _transaction: object,
        *,
        limit: int,
        after_generation_claim_expires_at: datetime | None = None,
        after_draft_id: str | None = None,
    ):
        claims = self.journal.expired_claims
        if after_generation_claim_expires_at is not None:
            claims = tuple(
                claim
                for claim in claims
                if (
                    claim.generation_claim_expires_at,
                    claim.draft_id,
                )
                > (after_generation_claim_expires_at, after_draft_id)
            )
        return claims[:limit]

    def reclaim_expired_generation_claim(
        self,
        _transaction: object,
        **values: Any,
    ):
        claim = self.journal.expired_claims[0]
        assert values["owner_id"] == claim.owner_id
        assert values["draft_id"] == claim.draft_id
        assert values["expected_revision"] == claim.state_revision
        assert values["claim_id"] == claim.generation_claim_id
        reclaimed = SimpleNamespace(
            **{
                **vars(claim),
                "state_revision": claim.state_revision + 1,
                "generation_claim_expires_at": datetime.now(UTC)
                + timedelta(seconds=values["lease_seconds"]),
            }
        )
        self.journal.reclaimed_expired_claim = reclaimed
        return reclaimed

    def finish_generation(self, _transaction: object, **values: Any):
        reclaimed = self.journal.reclaimed_expired_claim
        assert reclaimed is not None
        assert values["owner_id"] == reclaimed.owner_id
        assert values["draft_id"] == reclaimed.draft_id
        assert values["expected_revision"] == reclaimed.state_revision
        assert values["claim_id"] == reclaimed.generation_claim_id
        assert values["status"] == "error"
        self.journal.expired_finish_entered.set()
        self.journal.expired_finish_release.wait(2)
        finished = SimpleNamespace(
            **{
                **vars(reclaimed),
                "state_revision": reclaimed.state_revision + 1,
                "generation_claim_id": None,
                "generation_claim_expires_at": None,
                "status": "error",
                "error_message": values["error_message"],
            }
        )
        self.journal.finished_expired_claim = finished
        self.journal.expired_claims = ()
        return finished


class _FakeStore:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.threads: list[int] = []
        self.stage_entered = threading.Event()
        self.stage_release = threading.Event()
        self.stage_release.set()
        self.promote_entered = threading.Event()
        self.promote_release = threading.Event()
        self.promote_release.set()
        self.stage_count = 0
        self.quarantined_staged = 0
        self.quarantined_published = 0
        self.recovery: BundleRecoveryResult | None = None
        self.published: PublishedBundle | None = None
        self.stage_error: BaseException | None = None
        self.quarantine_staged_error: BaseException | None = None
        self.quarantine_receipt_error: BaseException | None = None
        self.quarantine_receipt_entered = threading.Event()
        self.quarantine_receipt_release = threading.Event()
        self.quarantine_receipt_release.set()
        self.recover_error: BaseException | None = None
        self.recover_entered = threading.Event()
        self.recover_release = threading.Event()
        self.recover_release.set()

    def stage(
        self, finalized: FinalizedBundle, *, key: Any, fence_check, **_values: Any
    ):
        self.threads.append(threading.get_ident())
        self.events.append("stage")
        self.stage_count += 1
        fence_check("before_stage")
        if self.stage_error is not None:
            raise self.stage_error
        self.stage_entered.set()
        self.stage_release.wait(2)
        return StagedBundleReceipt(
            paths=paths_for(key),
            publication_key=key,
            storage_identity=object(),  # type: ignore[arg-type]
            bundle_sha256=finalized.bundle_sha256,
            manifest_sha256=hashlib.sha256(
                finalized.manifest_json.encode()
            ).hexdigest(),
            runtime_metadata=finalized.runtime_metadata,
        )

    def promote_staged(
        self, receipt: StagedBundleReceipt, *, fence_check, **_values: Any
    ):
        self.threads.append(threading.get_ident())
        self.events.append("promote")
        fence_check("before_replace")
        self.published = self._published(receipt)
        self.promote_entered.set()
        self.promote_release.wait(2)
        return self.published

    def quarantine_staged(self, _receipt: StagedBundleReceipt) -> None:
        self.threads.append(threading.get_ident())
        self.events.append("quarantine_staged")
        if self.quarantine_staged_error is not None:
            raise self.quarantine_staged_error
        self.quarantined_staged += 1

    def quarantine_receipt(self, _receipt: BundlePublicationReceipt) -> None:
        self.threads.append(threading.get_ident())
        self.events.append("quarantine_published")
        self.quarantine_receipt_entered.set()
        self.quarantine_receipt_release.wait(2)
        if self.quarantine_receipt_error is not None:
            raise self.quarantine_receipt_error
        self.quarantined_published += 1

    def recover(self, **_values: Any):
        self.threads.append(threading.get_ident())
        self.recover_entered.set()
        self.recover_release.wait(2)
        if self.recover_error is not None:
            raise self.recover_error
        if self.recovery is None:
            raise AssertionError("test did not configure recovery disposition")
        return self.recovery

    def load(self, _path: str, **_values: Any):
        self.threads.append(threading.get_ident())
        if self.published is None:
            raise AssertionError("published bundle is unavailable")
        return self.published

    def _published(self, receipt: StagedBundleReceipt) -> PublishedBundle:
        publication_receipt = BundlePublicationReceipt(
            paths=receipt.paths,
            publication_key=receipt.publication_key,
            storage_identity=receipt.storage_identity,
            bundle_sha256=receipt.bundle_sha256,
            manifest_sha256=receipt.manifest_sha256,
        )
        request_bundle = _request().bundle
        return PublishedBundle(
            bundle_relative_path=receipt.paths.revision_relative_path,
            bundle_sha256=receipt.bundle_sha256,
            manifest_sha256=receipt.manifest_sha256,
            files=request_bundle.files,
            manifest=request_bundle.manifest,
            manifest_json=request_bundle.manifest_json,
            runtime_metadata=request_bundle.runtime_metadata,
            storage_identity=receipt.storage_identity,
            receipt=publication_receipt,
        )


def _service(
    *,
    journal: _FakeJournal | None = None,
    store: _FakeStore | None = None,
    admission: WorkAdmissionCoordinator | None = None,
    heartbeat: float = 10.0,
    recovery_batch_size: int = 100,
):
    journal = journal or _FakeJournal()
    store = store or _FakeStore()
    runtime = _FakeRuntime()
    repositories = SimpleNamespace(
        generated_agent_publications=journal,
        agents=_FakeAgents(journal),
        draft_agents=_FakeDrafts(journal),
    )
    admission = admission or _coordinator()
    service = GeneratedAgentPublicationService(
        plane_runtime=runtime,
        plane_repositories=repositories,
        bundle_store=store,
        work_admission=admission,
        heartbeat_interval_seconds=heartbeat,
        recovery_interval_seconds=0.01,
        recovery_batch_size=recovery_batch_size,
    )
    return service, journal, store, runtime, admission


def test_publication_identity_is_replay_stable_and_uuid4_shaped() -> None:
    first = _identity()
    assert _identity() == first
    assert len(set(astuple(first))) == 5
    for value in astuple(first):
        assert isinstance(value, uuid.UUID)
        assert value.version == 4


@pytest.mark.parametrize(
    "changed",
    (
        {"owner_id": "other-owner"},
        {"draft_uuid": "30000000-0000-4000-8000-000000000074"},
        {"source_state_revision": 8},
        {"generation_claim_id": "40000000-0000-4000-8000-000000000074"},
        {"target_agent_id": "other-agent"},
    ),
)
def test_each_authority_field_changes_every_derived_identity(
    changed: dict[str, object],
) -> None:
    baseline = _identity()
    candidate = _identity(**changed)
    assert candidate != baseline
    assert set(astuple(candidate)).isdisjoint(astuple(baseline))


@pytest.mark.parametrize(
    ("changed", "message"),
    (
        ({"owner_id": ""}, "owner_id"),
        ({"draft_uuid": "not-a-uuid"}, "must be UUIDs"),
        ({"draft_uuid": str(uuid.uuid1())}, "draft_uuid must be a UUID4"),
        ({"generation_claim_id": str(uuid.uuid1())}, "generation_claim_id"),
        ({"source_state_revision": True}, "non-negative"),
        ({"source_state_revision": -1}, "non-negative"),
        ({"target_agent_id": "../escape"}, "safe bounded"),
    ),
)
def test_publication_identity_rejects_ambiguous_or_unsafe_inputs(
    changed: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _identity(**changed)


@pytest.mark.parametrize(
    ("changes", "error"),
    (
        ({"bundle": object()}, TypeError),
        ({"runtime_contract_version": 0}, ValueError),
        ({"release_lock_digest": "not-a-digest"}, ValueError),
        ({"generation_result": object()}, TypeError),
    ),
)
def test_publication_request_rejects_untyped_contract_inputs(
    changes: dict[str, object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        _request(**changes)


@pytest.mark.asyncio
async def test_same_bundle_submit_ack_loss_reclaims_exact_operation_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, journal, _store, _runtime, admission = _service()
    original_submit = admission.submit
    submitted_operation_ids: list[uuid.UUID] = []

    def lose_first_ack(request: Any):
        admitted = original_submit(request)
        submitted_operation_ids.append(admitted.operation_id)
        if len(submitted_operation_ids) == 1:
            raise RuntimeError("submission acknowledgement lost")
        return admitted

    monkeypatch.setattr(admission, "submit", lose_first_ack)
    request = _request()
    with pytest.raises(GeneratedAgentPublicationPreIntentError):
        await service.publish(request)
    await asyncio.sleep(0)

    result = await service.publish(request)

    assert len(submitted_operation_ids) == 2
    assert len(set(submitted_operation_ids)) == 1
    assert result.publication.operation_id == str(submitted_operation_ids[0])
    assert journal.events.count("begin") == 1
    operation = admission.repository.get_operation_for_administration(
        submitted_operation_ids[0]
    )
    assert operation is not None and operation.state is OperationState.COMPLETED
    await service.close()


@pytest.mark.asyncio
async def test_claim_commit_ack_loss_reselects_once_before_journal_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, journal, _store, _runtime, admission = _service()
    original_claim = admission.claim_operation
    ambiguous_claims: list[Any] = []

    def commit_then_lose_ack(*args: Any, **kwargs: Any):
        claim = original_claim(*args, **kwargs)
        assert claim is not None
        ambiguous_claims.append(claim)
        raise RuntimeError("claim acknowledgement lost")

    monkeypatch.setattr(admission, "claim_operation", commit_then_lose_ack)

    result = await service.publish(_request())

    assert len(ambiguous_claims) == 1
    ambiguous = ambiguous_claims[0]
    assert result.publication.operation_id == str(ambiguous.fence.operation_id)
    assert (
        result.publication.operation_execution_generation
        == ambiguous.fence.execution_generation + 1
    )
    assert journal.events.count("begin") == 1
    operation = admission.repository.get_operation_for_administration(
        ambiguous.fence.operation_id
    )
    assert operation is not None and operation.state is OperationState.COMPLETED
    assert (
        operation.execution_generation
        == result.publication.operation_execution_generation
    )
    await service.close()


@pytest.mark.asyncio
async def test_uncertain_claim_concurrent_reselection_poison_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, journal, _store, _runtime, admission = _service()
    request = _request()
    identity = _identity()
    original_claim = admission.claim_operation
    original_reselect = admission.reselect_execution
    ambiguous_claims: list[Any] = []
    concurrent_fences: list[Any] = []

    def commit_then_lose_ack(*args: Any, **kwargs: Any):
        claim = original_claim(*args, **kwargs)
        assert claim is not None
        ambiguous_claims.append(claim)
        raise RuntimeError("claim acknowledgement lost")

    def poison_reselection(fence: Any):
        concurrent_fences.append(original_reselect(fence))
        return original_reselect(fence)

    monkeypatch.setattr(admission, "claim_operation", commit_then_lose_ack)
    monkeypatch.setattr(admission, "reselect_execution", poison_reselection)

    with pytest.raises(
        GeneratedAgentPublicationPreIntentError,
        match="claim outcome could not be reconciled exactly",
    ):
        await service._admit_and_claim(
            binding=_binding(request),
            owner_id=request.owner_id,
            submission_id=identity.submission_id,
            request_generation=identity.request_generation,
        )

    assert len(ambiguous_claims) == 1
    assert len(concurrent_fences) == 1
    operation = admission.repository.get_operation_for_administration(
        ambiguous_claims[0].fence.operation_id
    )
    assert operation is not None and operation.state is OperationState.RUNNING
    assert operation.execution_generation == concurrent_fences[0].execution_generation
    assert operation.execution_lease_token == concurrent_fences[0].execution_lease_token
    assert journal.publication is None
    assert journal.events == []
    await service.close()


@pytest.mark.asyncio
async def test_cancellation_during_uncertain_claim_reselection_stays_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _journal, _store, _runtime, admission = _service()
    request = _request()
    identity = _identity()
    original_claim = admission.claim_operation
    original_reselect = admission.reselect_execution
    reselect_entered = threading.Event()
    reselect_release = threading.Event()
    selected_fences: list[Any] = []
    attempt = SimpleNamespace(deep_fence=None)

    def commit_then_lose_ack(*args: Any, **kwargs: Any):
        claim = original_claim(*args, **kwargs)
        assert claim is not None
        raise RuntimeError("claim acknowledgement lost during cancellation")

    def blocked_reselection(fence: Any):
        selected = original_reselect(fence)
        selected_fences.append(selected)
        reselect_entered.set()
        reselect_release.wait(2)
        return selected

    monkeypatch.setattr(admission, "claim_operation", commit_then_lose_ack)
    monkeypatch.setattr(admission, "reselect_execution", blocked_reselection)
    task = asyncio.create_task(
        service._admit_and_claim(
            binding=_binding(request),
            owner_id=request.owner_id,
            submission_id=identity.submission_id,
            request_generation=identity.request_generation,
            attempt=attempt,
        )
    )
    assert await asyncio.to_thread(reselect_entered.wait, 1)

    task.cancel()
    await asyncio.sleep(0)
    reselect_release.set()
    with pytest.raises(asyncio.CancelledError) as captured:
        await task

    assert len(selected_fences) == 1
    assert attempt.deep_fence == selected_fences[0]
    assert isinstance(captured.value.__cause__, RuntimeError)
    assert "claim acknowledgement lost" in str(captured.value.__cause__)
    await service.close()


@pytest.mark.asyncio
async def test_changed_bundle_submit_ack_loss_replay_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, journal, _store, _runtime, admission = _service()
    original_submit = admission.submit
    submitted_operation_ids: list[uuid.UUID] = []

    def lose_first_ack(request: Any):
        admitted = original_submit(request)
        submitted_operation_ids.append(admitted.operation_id)
        if len(submitted_operation_ids) == 1:
            raise RuntimeError("submission acknowledgement lost")
        return admitted

    monkeypatch.setattr(admission, "submit", lose_first_ack)
    with pytest.raises(GeneratedAgentPublicationPreIntentError):
        await service.publish(_request())
    await asyncio.sleep(0)

    changed_files = dict(FILES)
    changed_files["agent_main.py"] = "changed main\n"
    identity = _identity()
    changed = _request(
        bundle=_bundle(str(identity.target_revision_id), files=changed_files)
    )
    with pytest.raises(GeneratedAgentPublicationPreIntentError) as captured:
        await service.publish(changed)

    causes: list[BaseException] = []
    cause: BaseException | None = captured.value
    while cause is not None and cause not in causes:
        causes.append(cause)
        cause = cause.__cause__
    assert any(
        "persisted operation identity did not match the exact request" in str(item)
        for item in causes
    )
    assert len(set(submitted_operation_ids)) == 1
    operation = admission.repository.get_operation_for_administration(
        submitted_operation_ids[0]
    )
    assert operation is not None and operation.state is OperationState.RUNNING
    assert journal.publication is None
    assert journal.events == []
    await service.close()


@pytest.mark.asyncio
async def test_poisoned_submission_mapping_fails_before_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _journal, _store, _runtime, admission = _service()
    request = _request()
    identity = _identity()
    original_reconcile = admission.reconcile_submission
    claim_calls = 0

    def poisoned_reconcile(**values: Any):
        submission = original_reconcile(**values)
        return replace(
            submission,
            operation=replace(submission.operation, operation_id=uuid.uuid4()),
        )

    def unexpected_claim(*_args: Any, **_kwargs: Any):
        nonlocal claim_calls
        claim_calls += 1
        raise AssertionError("poisoned submission must not be claimed")

    monkeypatch.setattr(admission, "reconcile_submission", poisoned_reconcile)
    monkeypatch.setattr(admission, "claim_operation", unexpected_claim)
    with pytest.raises(
        GeneratedAgentPublicationPreIntentError,
        match="persisted submission identity",
    ):
        await service._admit_and_claim(
            binding=_binding(request),
            owner_id=request.owner_id,
            submission_id=identity.submission_id,
            request_generation=identity.request_generation,
        )
    assert claim_calls == 0
    await service.close()


@pytest.mark.parametrize(
    ("field_name", "poisoned_value"),
    (
        ("operation_id", uuid.UUID("81000000-0000-4000-8000-000000000074")),
        ("operation_kind", "poisoned_publication"),
        ("admission_class", AdmissionClass.GLOBAL),
        ("owner_scope", OwnerScope.CONNECTION),
        ("owner_user_id", "other-owner"),
        (
            "connection_scope_id",
            uuid.UUID("82000000-0000-4000-8000-000000000074"),
        ),
        ("idempotency_namespace", "poisoned.namespace"),
        ("idempotency_key", "poisoned-key"),
        ("normalized_input_digest", "f" * 64),
        ("chat_id", "poisoned-chat"),
        (
            "parent_operation_id",
            uuid.UUID("83000000-0000-4000-8000-000000000074"),
        ),
        (
            "connection_generation",
            uuid.UUID("84000000-0000-4000-8000-000000000074"),
        ),
        (
            "request_generation",
            uuid.UUID("85000000-0000-4000-8000-000000000074"),
        ),
    ),
)
@pytest.mark.asyncio
async def test_poisoned_persisted_operation_identity_fails_before_claim(
    field_name: str,
    poisoned_value: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _journal, _store, _runtime, admission = _service()
    request = _request()
    identity = _identity()
    repository = admission.repository
    original_get = repository.get_operation_for_administration
    claim_calls = 0

    def poisoned_get(operation_id: uuid.UUID, **values: Any):
        operation = original_get(operation_id, **values)
        assert operation is not None
        return replace(operation, **{field_name: poisoned_value})

    def unexpected_claim(*_args: Any, **_kwargs: Any):
        nonlocal claim_calls
        claim_calls += 1
        raise AssertionError("poisoned operation must not be claimed")

    monkeypatch.setattr(repository, "get_operation_for_administration", poisoned_get)
    monkeypatch.setattr(admission, "claim_operation", unexpected_claim)
    with pytest.raises(
        GeneratedAgentPublicationPreIntentError,
        match="persisted operation identity",
    ):
        await service._admit_and_claim(
            binding=_binding(request),
            owner_id=request.owner_id,
            submission_id=identity.submission_id,
            request_generation=identity.request_generation,
        )
    assert claim_calls == 0
    await service.close()


@pytest.mark.parametrize("poison", ("parent", "fence"))
@pytest.mark.asyncio
async def test_poisoned_claim_identity_fails_before_publication_intent(
    poison: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, journal, _store, _runtime, admission = _service()
    request = _request()
    identity = _identity()
    original_claim = admission.claim_operation
    claimed: list[Any] = []

    def poisoned_claim(*args: Any, **kwargs: Any):
        claim = original_claim(*args, **kwargs)
        assert claim is not None
        claimed.append(claim)
        if poison == "parent":
            return replace(
                claim,
                operation=replace(
                    claim.operation,
                    parent_operation_id=uuid.UUID(
                        "86000000-0000-4000-8000-000000000074"
                    ),
                ),
            )
        return replace(
            claim,
            fence=replace(
                claim.fence,
                execution_generation=claim.fence.execution_generation + 1,
            ),
        )

    monkeypatch.setattr(admission, "claim_operation", poisoned_claim)
    with pytest.raises(GeneratedAgentPublicationPreIntentError, match="claimed"):
        await service._admit_and_claim(
            binding=_binding(request),
            owner_id=request.owner_id,
            submission_id=identity.submission_id,
            request_generation=identity.request_generation,
        )

    operation = admission.repository.get_operation_for_administration(
        claimed[0].fence.operation_id
    )
    assert operation is not None and operation.state is OperationState.RUNNING
    assert operation.terminal_code is None
    assert journal.publication is None
    assert journal.events == []
    await service.close()


def test_terminal_reconciliation_rejects_missing_authoritative_records() -> None:
    service, _journal, _store, _runtime, _admission = _service()
    with pytest.raises(GeneratedAgentPublicationRecoveryPendingError):
        GeneratedAgentPublicationService._assert_same_publication(
            None,
            SimpleNamespace(),
        )
    with pytest.raises(GeneratedAgentPublicationRecoveryPendingError):
        GeneratedAgentPublicationService._assert_terminal_operation(
            None,
            SimpleNamespace(operation_id=uuid.uuid4(), execution_generation=1),
            state=OperationState.COMPLETED,
            terminal_code=None,
            safe_summary=None,
            retry_after_ms=None,
        )
    missing_fence = SimpleNamespace(deep_fence=None)
    with pytest.raises(GeneratedAgentPublicationRecoveryPendingError):
        service._journal_terminal_transition_sync(
            missing_fence,  # type: ignore[arg-type]
            "commit_published",
            operation_state=OperationState.COMPLETED,
            operation_terminal_code=None,
            operation_safe_summary=None,
            operation_retry_after_ms=None,
        )
    with pytest.raises(GeneratedAgentPublicationRecoveryPendingError):
        service._refresh_terminal_publication_sync(
            missing_fence,  # type: ignore[arg-type]
            publication_state="published",
            operation_state=OperationState.COMPLETED,
            operation_terminal_code=None,
            operation_safe_summary=None,
            operation_retry_after_ms=None,
        )
    with pytest.raises(GeneratedAgentPublicationRecoveryPendingError):
        service._assert_successor_terminal_operation(
            object(),
            publication=SimpleNamespace(operation_id="invalid"),
        )
    with pytest.raises(GeneratedAgentPublicationRecoveryPendingError):
        service._assert_successor_terminal_operation(
            object(),
            publication=SimpleNamespace(
                operation_id=str(uuid.uuid4()),
                operation_execution_generation=1,
                state="published",
                failure_code=None,
            ),
        )


@pytest.mark.parametrize(
    ("kwargs", "error"),
    (
        ({"renew": None}, TypeError),
        ({"interval_seconds": True}, TypeError),
        ({"interval_seconds": 0}, ValueError),
        ({"task_name": ""}, ValueError),
    ),
)
def test_heartbeat_rejects_invalid_construction(
    kwargs: dict[str, object], error: type[Exception]
) -> None:
    values: dict[str, object] = {
        "renew": lambda: object(),
        "interval_seconds": 1,
        "task_name": "valid",
    }
    values.update(kwargs)
    with pytest.raises(error):
        GenerationClaimHeartbeat(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_heartbeat_requires_start_and_refuses_double_start() -> None:
    heartbeat = GenerationClaimHeartbeat(
        lambda: object(), interval_seconds=10, task_name="start-contract"
    )
    with pytest.raises(RuntimeError, match="not started"):
        heartbeat.assert_healthy()
    await heartbeat.close()
    heartbeat.start()
    with pytest.raises(RuntimeError, match="already started"):
        heartbeat.start()
    await heartbeat.close()


@pytest.mark.asyncio
async def test_cancelled_heartbeat_task_is_reported_as_claim_loss() -> None:
    heartbeat = GenerationClaimHeartbeat(
        lambda: object(), interval_seconds=10, task_name="cancelled-heartbeat"
    )
    heartbeat.start()
    assert heartbeat._task is not None
    heartbeat._task.cancel()
    await asyncio.sleep(0)
    with pytest.raises(
        GenerationClaimLostError, match="(renewal failed|stopped unexpectedly)"
    ):
        heartbeat.assert_healthy()
    with pytest.raises(GenerationClaimLostError):
        await heartbeat.close()


@pytest.mark.asyncio
async def test_service_publishes_two_phase_off_loop_and_loads_terminal_replay() -> None:
    service, journal, store, runtime, admission = _service(heartbeat=0.01)
    caller_thread = threading.get_ident()

    result = await service.publish(_request())
    replay = await service.load_published(
        owner_id=OWNER,
        draft_uuid=DRAFT,
        source_state_revision=7,
    )

    assert result.claim_managed is True
    assert replay == result
    assert journal.publication is not None
    assert journal.publication.state == "published"
    assert journal.events[:4] == ["begin", "fence", "mark_staged", "mark_validated"]
    assert journal.events[-2:] == ["fence", "commit"]
    assert store.events == ["stage", "promote"]
    assert runtime.threads and set(runtime.threads) != {caller_thread}
    assert store.threads and set(store.threads) != {caller_thread}
    assert admission.inspect_admission_class(AdmissionClass.SYSTEM).active_count == 0
    await service.close()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("plane_runtime", object()),
        ("plane_repositories", SimpleNamespace()),
        ("bundle_store", object()),
        ("work_admission", object()),
        ("claim_lease_seconds", 0),
        ("heartbeat_interval_seconds", 0),
        ("recovery_interval_seconds", True),
        ("recovery_batch_size", 0),
    ),
)
def test_service_constructor_fails_closed_on_invalid_bindings(
    field: str, value: object
) -> None:
    journal = _FakeJournal()
    values: dict[str, object] = {
        "plane_runtime": _FakeRuntime(),
        "plane_repositories": SimpleNamespace(
            generated_agent_publications=journal,
            agents=_FakeAgents(journal),
            draft_agents=_FakeDrafts(journal),
        ),
        "bundle_store": _FakeStore(),
        "work_admission": _coordinator(),
    }
    values[field] = value
    with pytest.raises((TypeError, ValueError)):
        GeneratedAgentPublicationService(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_service_replays_terminal_without_second_stage_or_id_allocation() -> None:
    service, _journal, store, _runtime, _admission = _service()
    request = _request()
    first = await service.publish(request)
    second = await service.publish(request)
    assert second == first
    assert store.stage_count == 1
    with pytest.raises(GeneratedAgentPublicationManagedError):
        await service.publish(
            _request(
                generation_result=GeneratedAgentPublicationResultMetadata(
                    security_report="different"
                )
            )
        )
    with pytest.raises(
        GeneratedAgentPublicationManagedError,
        match="immutable request identity",
    ):
        await service.publish(replace(request, generation_claim_id=str(uuid.uuid4())))
    with pytest.raises(
        GeneratedAgentPublicationManagedError,
        match="immutable request identity",
    ):
        await service.publish(replace(request, runtime_contract_version=4))
    await service.close()


@pytest.mark.asyncio
async def test_load_published_distinguishes_absent_and_incomplete_provenance() -> None:
    service, journal, _store, _runtime, _admission = _service()
    assert (
        await service.load_published(
            owner_id=OWNER,
            draft_uuid=DRAFT,
            source_state_revision=7,
        )
        is None
    )
    await service.publish(_request())
    journal.missing_revision = True
    with pytest.raises(
        GeneratedAgentPublicationRecoveryPendingError, match="incomplete"
    ):
        await service.load_published(
            owner_id=OWNER,
            draft_uuid=DRAFT,
            source_state_revision=7,
        )
    journal.missing_revision = False
    assert journal.publication is not None
    journal.publication = replace(journal.publication, manifest_digest=None)
    with pytest.raises(
        GeneratedAgentPublicationRecoveryPendingError, match="provenance"
    ):
        await service.load_published(
            owner_id=OWNER,
            draft_uuid=DRAFT,
            source_state_revision=7,
        )
    await service.close()


@pytest.mark.parametrize(
    "revision_state",
    ("prepared", "starting", "ready", "active", "retired", "failed"),
)
@pytest.mark.asyncio
async def test_terminal_replay_accepts_exact_published_revision_lifecycle_states(
    revision_state: str,
) -> None:
    service, journal, _store, _runtime, _admission = _service()
    request = _request()
    await service.publish(request)
    assert journal.revision is not None
    journal.revision = replace(journal.revision, state=revision_state)

    replay = await service.load_published(
        owner_id=OWNER,
        draft_uuid=DRAFT,
        source_state_revision=7,
    )

    assert replay is not None and replay.revision.state == revision_state
    assert await service.publish(request) == replay
    await service.close()


@pytest.mark.parametrize("revision_state", ("deleted", "legacy_pending"))
@pytest.mark.asyncio
async def test_terminal_replay_rejects_non_publication_revision_states(
    revision_state: str,
) -> None:
    service, journal, _store, _runtime, _admission = _service()
    await service.publish(_request())
    assert journal.revision is not None
    journal.revision = replace(journal.revision, state=revision_state)

    with pytest.raises(
        GeneratedAgentPublicationRecoveryPendingError,
        match="terminal records",
    ):
        await service.load_published(
            owner_id=OWNER,
            draft_uuid=DRAFT,
            source_state_revision=7,
        )
    await service.close()


@pytest.mark.parametrize("corruption", ("incompatible", "deleted_agent"))
@pytest.mark.asyncio
async def test_terminal_replay_rejects_incompatible_or_deleted_agent_identity(
    corruption: str,
) -> None:
    service, journal, _store, _runtime, _admission = _service()
    await service.publish(_request())
    assert journal.revision is not None
    if corruption == "incompatible":
        journal.revision = replace(
            journal.revision,
            compatibility_state="incompatible",
        )
    else:
        journal.agent.deleted_at = datetime.now(UTC)

    with pytest.raises(
        GeneratedAgentPublicationRecoveryPendingError,
        match="terminal records",
    ):
        await service.load_published(
            owner_id=OWNER,
            draft_uuid=DRAFT,
            source_state_revision=7,
        )
    await service.close()


@pytest.mark.asyncio
async def test_service_start_is_idempotent_and_close_blocks_new_admission() -> None:
    service, _journal, _store, _runtime, _admission = _service()
    service.start()
    service.start()
    await asyncio.sleep(0.02)
    await service.close()
    await service.close()
    with pytest.raises(RuntimeError, match="closing"):
        service.start()
    with pytest.raises(GeneratedAgentPublicationPreIntentError):
        await service.publish(_request())


@pytest.mark.asyncio
async def test_publish_rejects_non_request_and_concurrent_input_conflict() -> None:
    service, journal, store, _runtime, _admission = _service()
    with pytest.raises(TypeError):
        await service.publish(object())  # type: ignore[arg-type]
    store.stage_release.clear()
    running = asyncio.create_task(service.publish(_request()))
    assert await asyncio.to_thread(store.stage_entered.wait, 1)
    changed = replace(_request(), compatibility_state="requires_migration")
    with pytest.raises(GeneratedAgentPublicationRecoveryPendingError) as captured:
        await service.publish(changed)
    assert captured.value.claim_managed is True
    if not captured.value.claim_managed:  # Mirrors lifecycle's caller-owned cleanup.
        journal.draft.generation_claim_id = None
    assert journal.draft.generation_claim_id == CLAIM
    store.stage_release.set()
    await running
    await service.close()


@pytest.mark.asyncio
async def test_preintent_failure_is_explicitly_caller_managed() -> None:
    service, _journal, _store, _runtime, _admission = _service(
        journal=_FakeJournal(begin_error=RuntimeError("begin rejected"))
    )
    with pytest.raises(GeneratedAgentPublicationPreIntentError) as captured:
        await service.publish(_request())
    assert captured.value.claim_managed is False
    await service.close()


@pytest.mark.asyncio
async def test_replay_inspection_failure_preserves_unproven_durable_claim() -> None:
    journal = _FakeJournal()
    _seed_recovery(journal, state="claimed")
    journal.get_error = RuntimeError("database unavailable")
    service, _journal, _store, _runtime, _admission = _service(journal=journal)
    with pytest.raises(GeneratedAgentPublicationRecoveryPendingError) as captured:
        await service.publish(_request())
    assert captured.value.claim_managed is True
    assert isinstance(captured.value.__cause__, RuntimeError)
    if not captured.value.claim_managed:  # Mirrors lifecycle's caller-owned cleanup.
        journal.draft.generation_claim_id = None
    assert journal.draft.generation_claim_id == CLAIM
    journal.get_error = None
    await service.close()


@pytest.mark.asyncio
async def test_cancelled_replay_lookup_error_preserves_unproven_durable_claim() -> None:
    journal = _FakeJournal()
    _seed_recovery(journal, state="claimed")
    journal.get_error = RuntimeError("database unavailable")
    journal.get_release.clear()
    service, _journal, _store, _runtime, _admission = _service(journal=journal)
    task = asyncio.create_task(service.publish(_request()))
    assert await asyncio.to_thread(journal.get_entered.wait, 1)

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    journal.get_release.set()
    with pytest.raises(asyncio.CancelledError) as captured:
        await task

    marker = captured.value.__cause__
    assert isinstance(marker, GeneratedAgentPublicationRecoveryPendingError)
    assert marker.claim_managed is True
    assert isinstance(marker.__cause__, RuntimeError)
    assert str(marker.__cause__) == "database unavailable"
    if not marker.claim_managed:  # Mirrors lifecycle's caller-owned cleanup.
        journal.draft.generation_claim_id = None
    assert journal.draft.generation_claim_id == CLAIM
    journal.get_error = None
    await service.close()


@pytest.mark.asyncio
async def test_cancellation_during_failed_admission_lookup_stays_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _FakeJournal()
    journal.get_error = RuntimeError("authoritative lookup failed")
    journal.get_release.clear()
    service, _journal, _store, _runtime, _admission = _service(journal=journal)

    async def no_terminal_replay(**_values: Any):
        return None

    async def admission_failed(**_values: Any):
        raise RuntimeError("admission failed")

    monkeypatch.setattr(service, "load_published", no_terminal_replay)
    monkeypatch.setattr(service, "_admit_and_claim", admission_failed)
    task = asyncio.create_task(service.publish(_request()))
    assert await asyncio.to_thread(journal.get_entered.wait, 1)

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    journal.get_release.set()
    with pytest.raises(asyncio.CancelledError) as captured:
        await task

    marker = captured.value.__cause__
    assert isinstance(marker, GeneratedAgentPublicationRecoveryPendingError)
    assert marker.claim_managed is True
    assert isinstance(marker.__cause__, RuntimeError)
    assert str(marker.__cause__) == "authoritative lookup failed"
    assert isinstance(marker.__cause__.__cause__, RuntimeError)
    assert str(marker.__cause__.__cause__) == "admission failed"
    if not marker.claim_managed:  # Mirrors lifecycle's caller-owned cleanup.
        journal.draft.generation_claim_id = None
    assert journal.draft.generation_claim_id == CLAIM
    await service.close()


@pytest.mark.asyncio
async def test_failed_admission_with_lookup_ambiguity_is_claim_managed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _FakeJournal()
    journal.get_error = RuntimeError("authoritative lookup failed")
    service, _journal, _store, _runtime, _admission = _service(journal=journal)

    async def no_terminal_replay(**_values: Any):
        return None

    async def admission_failed(**_values: Any):
        raise RuntimeError("admission failed")

    monkeypatch.setattr(service, "load_published", no_terminal_replay)
    monkeypatch.setattr(service, "_admit_and_claim", admission_failed)
    with pytest.raises(GeneratedAgentPublicationRecoveryPendingError) as captured:
        await service.publish(_request())

    assert captured.value.claim_managed is True
    assert isinstance(captured.value.__cause__, RuntimeError)
    if not captured.value.claim_managed:  # Mirrors lifecycle's caller-owned cleanup.
        journal.draft.generation_claim_id = None
    assert journal.draft.generation_claim_id == CLAIM
    await service.close()


@pytest.mark.asyncio
async def test_cancellation_during_preintent_terminalization_stays_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _FakeJournal(begin_error=RuntimeError("begin failed"))
    service, _journal, _store, _runtime, _admission = _service(journal=journal)
    terminal_entered = threading.Event()
    terminal_release = threading.Event()

    async def no_terminal_replay(**_values: Any):
        return None

    def blocked_terminalization(*_args: Any, **_values: Any):
        terminal_entered.set()
        terminal_release.wait(2)
        raise RuntimeError("terminalization failed")

    monkeypatch.setattr(service, "load_published", no_terminal_replay)
    monkeypatch.setattr(service, "_terminalize_operation_sync", blocked_terminalization)
    task = asyncio.create_task(service.publish(_request()))
    assert await asyncio.to_thread(terminal_entered.wait, 1)

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    terminal_release.set()
    with pytest.raises(asyncio.CancelledError) as captured:
        await task

    assert isinstance(captured.value.__cause__, RuntimeError)
    assert str(captured.value.__cause__) == "terminalization failed"
    assert isinstance(captured.value.__cause__.__cause__, RuntimeError)
    assert str(captured.value.__cause__.__cause__) == "begin failed"
    await service.close()


@pytest.mark.parametrize(
    "lookup_outcome",
    ("absent", "intent", "conflict", "error"),
)
@pytest.mark.asyncio
async def test_admission_cancellation_reconciles_authoritative_intent(
    lookup_outcome: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _FakeJournal()
    if lookup_outcome == "intent":
        _seed_recovery(journal, state="claimed")
    elif lookup_outcome == "conflict":
        journal.get_error = GeneratedAgentPublicationRecoveryPendingError(
            "source conflict"
        )
    elif lookup_outcome == "error":
        journal.get_error = RuntimeError("lookup failed")
    service, _journal, _store, _runtime, _admission = _service(journal=journal)

    async def no_terminal_replay(**_values: Any):
        return None

    async def cancelled_admission(**_values: Any):
        raise asyncio.CancelledError()

    monkeypatch.setattr(service, "load_published", no_terminal_replay)
    monkeypatch.setattr(service, "_admit_and_claim", cancelled_admission)
    with pytest.raises(asyncio.CancelledError) as captured:
        await service.publish(_request())
    assert isinstance(captured.value, asyncio.CancelledError)
    await service.close()


@pytest.mark.asyncio
async def test_begin_cancellation_without_intent_terminalizes_claimed_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _journal, _store, _runtime, admission = _service()
    terminal_states: list[OperationState] = []
    original_terminalize = admission.terminalize

    async def cancelled_begin(**_values: Any):
        raise asyncio.CancelledError()

    def terminalize(*args: Any, **kwargs: Any):
        terminal_states.append(kwargs["state"])
        return original_terminalize(*args, **kwargs)

    monkeypatch.setattr(service, "_begin_intent", cancelled_begin)
    monkeypatch.setattr(admission, "terminalize", terminalize)
    with pytest.raises(asyncio.CancelledError):
        await service.publish(_request())

    assert terminal_states == [OperationState.CANCELLED]
    await service.close()


@pytest.mark.asyncio
async def test_begin_commit_ack_loss_and_failed_readback_remains_claim_managed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _FakeJournal()
    journal.begin_after_state_error = RuntimeError("begin acknowledgement lost")
    journal.get_error = RuntimeError("intent readback unavailable")
    service, _journal, _store, _runtime, _admission = _service(journal=journal)

    async def no_terminal_replay(**_values: Any):
        return None

    monkeypatch.setattr(service, "load_published", no_terminal_replay)
    with pytest.raises(GeneratedAgentPublicationRecoveryPendingError) as captured:
        await service.publish(_request())

    assert captured.value.claim_managed is True
    assert journal.publication is not None and journal.publication.state == "claimed"
    if not captured.value.claim_managed:  # Mirrors lifecycle's caller-owned cleanup.
        journal.draft.generation_claim_id = None
    assert journal.draft.generation_claim_id == CLAIM
    await service.close()


@pytest.mark.asyncio
async def test_begin_commit_cancel_and_failed_readback_keeps_cancel_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _FakeJournal()
    journal.begin_after_state_error = RuntimeError("begin acknowledgement lost")
    journal.begin_release.clear()
    service, _journal, _store, _runtime, _admission = _service(journal=journal)

    async def no_terminal_replay(**_values: Any):
        return None

    monkeypatch.setattr(service, "load_published", no_terminal_replay)
    task = asyncio.create_task(service.publish(_request()))
    assert await asyncio.to_thread(journal.begin_entered.wait, 1)
    journal.get_error = RuntimeError("intent readback unavailable")
    task.cancel()
    journal.begin_release.set()
    with pytest.raises(asyncio.CancelledError) as captured:
        await task

    marker = captured.value.__cause__
    assert isinstance(marker, GeneratedAgentPublicationRecoveryPendingError)
    assert marker.claim_managed is True
    assert journal.publication is not None and journal.publication.state == "claimed"
    if not marker.claim_managed:  # Mirrors lifecycle's caller-owned cleanup.
        journal.draft.generation_claim_id = None
    assert journal.draft.generation_claim_id == CLAIM
    await service.close()


@pytest.mark.asyncio
async def test_cancelled_begin_ack_is_discovered_and_managed() -> None:
    journal = _FakeJournal()
    journal.begin_release.clear()
    service, _journal, _store, _runtime, _admission = _service(journal=journal)
    task = asyncio.create_task(service.publish(_request()))
    assert await asyncio.to_thread(journal.begin_entered.wait, 1)
    task.cancel()
    journal.begin_release.set()
    with pytest.raises(asyncio.CancelledError) as captured:
        await task
    assert isinstance(
        captured.value.__cause__, GeneratedAgentPublicationManagedCancellation
    )
    assert journal.publication is not None and journal.publication.state == "failed"
    await service.close()


@pytest.mark.asyncio
async def test_stage_failure_terminalizes_managed_claim() -> None:
    store = _FakeStore()
    store.stage_error = RuntimeError("stage failed")
    service, journal, _store, _runtime, _admission = _service(store=store)
    with pytest.raises(GeneratedAgentPublicationManagedError) as captured:
        await service.publish(_request())
    assert captured.value.claim_managed is True
    assert journal.publication is not None and journal.publication.state == "failed"
    await service.close()


@pytest.mark.asyncio
async def test_managed_failure_state_then_ack_error_reconciles_failed_operation() -> (
    None
):
    journal = _FakeJournal()
    journal.fail_after_state_error = RuntimeError("failure acknowledgement lost")
    store = _FakeStore()
    store.stage_error = RuntimeError("stage failed")
    service, _journal, _store, _runtime, admission = _service(
        journal=journal,
        store=store,
    )

    with pytest.raises(GeneratedAgentPublicationManagedError):
        await service.publish(_request())

    assert journal.publication is not None
    assert journal.publication.state == "failed"
    assert journal.publication.operation_id is not None
    operation = admission.repository.get_operation_for_administration(
        uuid.UUID(journal.publication.operation_id)
    )
    assert operation is not None and operation.state is OperationState.FAILED
    await service.close()


@pytest.mark.asyncio
async def test_failed_transition_repairs_operation_terminalization_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeStore()
    store.stage_error = RuntimeError("stage failed")
    service, journal, _store, runtime, admission = _service(store=store)
    original_terminalize = admission.terminalize
    terminal_transactions: list[object] = []

    def fail_first_terminalize(*args: Any, **kwargs: Any):
        terminal_transactions.append(kwargs["transaction"])
        if len(terminal_transactions) == 1:
            raise RuntimeError("operation terminalization unavailable")
        return original_terminalize(*args, **kwargs)

    monkeypatch.setattr(admission, "terminalize", fail_first_terminalize)
    with pytest.raises(GeneratedAgentPublicationManagedError):
        await service.publish(_request())

    assert journal.publication is not None and journal.publication.state == "failed"
    assert terminal_transactions[0] is journal.terminal_transactions[0]
    assert all(transaction in runtime.transactions for transaction in terminal_transactions)
    assert journal.publication.operation_id is not None
    operation = admission.repository.get_operation_for_administration(
        uuid.UUID(journal.publication.operation_id)
    )
    assert operation is not None and operation.state is OperationState.FAILED
    assert (await service.readiness()).ready is True
    await service.close()


@pytest.mark.asyncio
async def test_cancellation_during_managed_failure_cleanup_stays_primary() -> None:
    journal = _FakeJournal()
    journal.fail_error = RuntimeError("journal failure acknowledgement lost")
    journal.fail_release.clear()
    store = _FakeStore()
    store.stage_error = RuntimeError("stage failed")
    service, _journal, _store, _runtime, _admission = _service(
        journal=journal,
        store=store,
    )
    task = asyncio.create_task(service.publish(_request()))
    assert await asyncio.to_thread(journal.fail_entered.wait, 1)

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    journal.fail_release.set()
    with pytest.raises(asyncio.CancelledError) as captured:
        await task

    assert isinstance(
        captured.value.__cause__, GeneratedAgentPublicationManagedCancellation
    )
    assert isinstance(captured.value.__cause__.__cause__, RuntimeError)
    assert (
        str(captured.value.__cause__.__cause__)
        == "journal failure acknowledgement lost"
    )
    assert journal.publication is not None and journal.publication.state == "claimed"
    await service.close()


@pytest.mark.asyncio
async def test_combined_claim_and_operation_heartbeat_runs_during_stage() -> None:
    store = _FakeStore()
    store.stage_release.clear()
    service, journal, _store, _runtime, _admission = _service(
        store=store, heartbeat=0.01
    )
    task = asyncio.create_task(service.publish(_request()))
    assert await asyncio.to_thread(store.stage_entered.wait, 1)
    for _ in range(100):
        if journal.renewals:
            break
        await asyncio.sleep(0.01)
    assert journal.renewals > 0
    store.stage_release.set()
    assert (await task).publication.state == "published"
    await service.close()


@pytest.mark.asyncio
async def test_heartbeat_authority_loss_fails_before_promotion() -> None:
    journal = _FakeJournal()
    journal.renew_error = RuntimeError("claim superseded")
    store = _FakeStore()
    store.stage_release.clear()
    service, _journal, _store, _runtime, _admission = _service(
        journal=journal, store=store, heartbeat=0.01
    )
    task = asyncio.create_task(service.publish(_request()))
    assert await asyncio.to_thread(store.stage_entered.wait, 1)
    for _ in range(100):
        if journal.renewals:
            break
        await asyncio.sleep(0.01)
    store.stage_release.set()
    with pytest.raises(GeneratedAgentPublicationManagedError):
        await task
    assert store.quarantined_staged == 1
    assert "promote" not in store.events
    await service.close()


@pytest.mark.asyncio
async def test_failure_terminalization_ambiguity_remains_recovery_pending() -> None:
    journal = _FakeJournal()
    journal.fail_error = RuntimeError("failure commit lost")
    store = _FakeStore()
    store.stage_error = RuntimeError("stage failed")
    service, _journal, _store, _runtime, _admission = _service(
        journal=journal, store=store
    )
    with pytest.raises(GeneratedAgentPublicationRecoveryPendingError):
        await service.publish(_request())
    assert journal.publication is not None and journal.publication.state == "claimed"
    await service.close()


@pytest.mark.asyncio
async def test_quarantine_failure_never_terminal_excludes_live_stage() -> None:
    store = _FakeStore()
    store.stage_release.clear()
    store.quarantine_staged_error = RuntimeError("quarantine unavailable")
    service, journal, _store, _runtime, _admission = _service(store=store)
    task = asyncio.create_task(service.publish(_request()))
    assert await asyncio.to_thread(store.stage_entered.wait, 1)
    task.cancel()
    store.stage_release.set()
    with pytest.raises(asyncio.CancelledError) as captured:
        await task
    assert isinstance(
        captured.value.__cause__, GeneratedAgentPublicationManagedCancellation
    )
    assert isinstance(captured.value.__cause__.__cause__, RuntimeError)
    assert str(captured.value.__cause__.__cause__) == "quarantine unavailable"
    assert journal.publication is not None and journal.publication.state == "claimed"
    assert "fail" not in journal.events
    await service.close()


@pytest.mark.asyncio
async def test_pre_promote_cancellation_quarantines_stage_then_fails_journal() -> None:
    store = _FakeStore()
    store.stage_release.clear()
    service, journal, _store, _runtime, _admission = _service(store=store)
    task = asyncio.create_task(service.publish(_request()))
    assert await asyncio.to_thread(store.stage_entered.wait, 1)

    task.cancel()
    store.stage_release.set()
    with pytest.raises(asyncio.CancelledError) as captured:
        await task

    assert isinstance(
        captured.value.__cause__, GeneratedAgentPublicationManagedCancellation
    )
    assert captured.value.__cause__.claim_managed is True
    assert store.quarantined_staged == 1
    assert journal.publication is not None and journal.publication.state == "failed"
    assert journal.events[-1] == "fail"
    await service.close()


@pytest.mark.asyncio
async def test_cancelled_failure_state_then_ack_error_reconciles_cancelled_operation() -> (
    None
):
    journal = _FakeJournal()
    journal.fail_after_state_error = RuntimeError("cancel acknowledgement lost")
    store = _FakeStore()
    store.stage_release.clear()
    service, _journal, _store, _runtime, admission = _service(
        journal=journal,
        store=store,
    )
    task = asyncio.create_task(service.publish(_request()))
    assert await asyncio.to_thread(store.stage_entered.wait, 1)

    task.cancel()
    store.stage_release.set()
    with pytest.raises(asyncio.CancelledError) as captured:
        await task

    assert isinstance(
        captured.value.__cause__, GeneratedAgentPublicationManagedCancellation
    )
    assert journal.publication is not None
    assert journal.publication.state == "failed"
    assert journal.publication.operation_id is not None
    operation = admission.repository.get_operation_for_administration(
        uuid.UUID(journal.publication.operation_id)
    )
    assert operation is not None and operation.state is OperationState.CANCELLED
    await service.close()


@pytest.mark.asyncio
async def test_post_native_cancellation_commits_db_before_rethrow() -> None:
    store = _FakeStore()
    store.promote_release.clear()
    service, journal, _store, _runtime, _admission = _service(store=store)
    task = asyncio.create_task(service.publish(_request()))
    assert await asyncio.to_thread(store.promote_entered.wait, 1)

    task.cancel()
    store.promote_release.set()
    with pytest.raises(asyncio.CancelledError) as captured:
        await task

    assert isinstance(
        captured.value.__cause__, GeneratedAgentPublicationManagedCancellation
    )
    assert journal.publication is not None and journal.publication.state == "published"
    assert store.quarantined_staged == 0
    await service.close()


@pytest.mark.asyncio
async def test_post_native_cancel_keeps_cancel_primary_when_db_commit_is_ambiguous() -> (
    None
):
    journal = _FakeJournal()
    journal.commit_error = RuntimeError("database acknowledgement lost")
    store = _FakeStore()
    store.promote_release.clear()
    service, _journal, _store, _runtime, _admission = _service(
        journal=journal, store=store
    )
    task = asyncio.create_task(service.publish(_request()))
    assert await asyncio.to_thread(store.promote_entered.wait, 1)
    task.cancel()
    store.promote_release.set()
    with pytest.raises(asyncio.CancelledError) as captured:
        await task
    assert isinstance(
        captured.value.__cause__, GeneratedAgentPublicationRecoveryPendingError
    )
    assert isinstance(captured.value.__cause__.__cause__, RuntimeError)
    assert journal.publication is not None and journal.publication.state == "validated"
    await service.close()


@pytest.mark.asyncio
async def test_post_native_cancel_reconciles_commit_state_then_ack_error() -> None:
    journal = _FakeJournal()
    journal.commit_after_state_error = RuntimeError("commit acknowledgement lost")
    store = _FakeStore()
    store.promote_release.clear()
    service, _journal, _store, _runtime, admission = _service(
        journal=journal,
        store=store,
    )
    task = asyncio.create_task(service.publish(_request()))
    assert await asyncio.to_thread(store.promote_entered.wait, 1)

    task.cancel()
    store.promote_release.set()
    with pytest.raises(asyncio.CancelledError) as captured:
        await task

    assert isinstance(
        captured.value.__cause__, GeneratedAgentPublicationManagedCancellation
    )
    assert journal.publication is not None and journal.publication.state == "published"
    assert journal.publication.operation_id is not None
    operation = admission.repository.get_operation_for_administration(
        uuid.UUID(journal.publication.operation_id)
    )
    assert operation is not None and operation.state is OperationState.COMPLETED
    assert journal.events.count("commit") == 1
    await service.close()


@pytest.mark.asyncio
async def test_cancellation_during_commit_ack_replays_terminal_state_without_failure() -> (
    None
):
    journal = _FakeJournal()
    journal.commit_release.clear()
    service, _journal, store, _runtime, admission = _service(journal=journal)
    task = asyncio.create_task(service.publish(_request()))
    assert await asyncio.to_thread(journal.commit_entered.wait, 1)
    task.cancel()
    journal.commit_release.set()
    with pytest.raises(asyncio.CancelledError) as captured:
        await task
    assert isinstance(
        captured.value.__cause__, GeneratedAgentPublicationManagedCancellation
    )
    assert journal.publication is not None and journal.publication.state == "published"
    assert journal.publication.operation_id is not None
    operation = admission.repository.get_operation_for_administration(
        uuid.UUID(journal.publication.operation_id)
    )
    assert operation is not None and operation.state is OperationState.COMPLETED
    assert journal.events.count("commit") == 1
    assert store.quarantined_staged == 0
    await service.close()


@pytest.mark.asyncio
async def test_commit_state_then_ack_error_reconciles_exact_published_result() -> None:
    journal = _FakeJournal()
    journal.commit_after_state_error = RuntimeError("commit acknowledgement lost")
    service, _journal, _store, _runtime, admission = _service(journal=journal)

    result = await service.publish(_request())

    assert result.publication.state == "published"
    assert journal.events.count("commit") == 1
    assert (
        journal.publication is not None and journal.publication.operation_id is not None
    )
    operation = admission.repository.get_operation_for_administration(
        uuid.UUID(journal.publication.operation_id)
    )
    assert operation is not None and operation.state is OperationState.COMPLETED
    await service.close()


@pytest.mark.asyncio
async def test_published_transition_repairs_operation_terminalization_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, journal, _store, runtime, admission = _service()
    original_terminalize = admission.terminalize
    terminal_transactions: list[object] = []

    def fail_first_terminalize(*args: Any, **kwargs: Any):
        terminal_transactions.append(kwargs["transaction"])
        if len(terminal_transactions) == 1:
            raise RuntimeError("operation terminalization unavailable")
        return original_terminalize(*args, **kwargs)

    monkeypatch.setattr(admission, "terminalize", fail_first_terminalize)
    result = await service.publish(_request())

    assert result.publication.state == "published"
    assert terminal_transactions[0] is journal.terminal_transactions[0]
    assert all(transaction in runtime.transactions for transaction in terminal_transactions)
    operation = admission.repository.get_operation_for_administration(
        uuid.UUID(result.publication.operation_id)
    )
    assert operation is not None and operation.state is OperationState.COMPLETED
    assert (await service.readiness()).ready is True
    await service.close()


@pytest.mark.asyncio
async def test_post_promote_commit_ambiguity_is_recovery_pending() -> None:
    journal = _FakeJournal()
    journal.commit_error = RuntimeError("commit acknowledgement lost")
    service, _journal, store, _runtime, _admission = _service(journal=journal)

    with pytest.raises(GeneratedAgentPublicationRecoveryPendingError) as captured:
        await service.publish(_request())

    assert captured.value.claim_managed is True
    assert journal.publication is not None and journal.publication.state == "validated"
    assert store.published is not None
    await service.close()


@pytest.mark.asyncio
async def test_concurrent_callers_join_one_publication() -> None:
    store = _FakeStore()
    store.stage_release.clear()
    service, _journal, _store, _runtime, _admission = _service(store=store)
    first = asyncio.create_task(service.publish(_request()))
    assert await asyncio.to_thread(store.stage_entered.wait, 1)
    second = asyncio.create_task(service.publish(_request()))
    await asyncio.sleep(0)
    store.stage_release.set()

    first_result, second_result = await asyncio.gather(first, second)
    assert first_result == second_result
    assert store.stage_count == 1
    await service.close()


@pytest.mark.asyncio
async def test_one_cancelled_joiner_does_not_revoke_remaining_caller() -> None:
    store = _FakeStore()
    store.stage_release.clear()
    service, _journal, _store, _runtime, _admission = _service(store=store)
    survivor = asyncio.create_task(service.publish(_request()))
    assert await asyncio.to_thread(store.stage_entered.wait, 1)
    cancelled = asyncio.create_task(service.publish(_request()))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    store.stage_release.set()
    assert (await survivor).publication.state == "published"
    assert store.quarantined_staged == 0
    await service.close()


@pytest.mark.asyncio
async def test_prejournal_cancelled_joiner_cannot_revoke_survivor_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, journal, _store, _runtime, _admission = _service()
    admission_entered = asyncio.Event()
    admission_release = asyncio.Event()
    original_admit = service._admit_and_claim

    async def blocked_admission(**values: Any):
        admission_entered.set()
        await admission_release.wait()
        return await original_admit(**values)

    monkeypatch.setattr(service, "_admit_and_claim", blocked_admission)
    survivor = asyncio.create_task(service.publish(_request()))
    await admission_entered.wait()
    cancelled = asyncio.create_task(service.publish(_request()))

    async def joined_attempt():
        while True:
            attempt = next(iter(service._attempts.values()))
            if attempt.waiters == 2:
                return attempt
            await asyncio.sleep(0)

    attempt = await asyncio.wait_for(joined_attempt(), timeout=5.0)
    assert attempt.publication is None and attempt.waiters == 2

    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError) as captured:
        await cancelled

    marker = captured.value.__cause__
    assert isinstance(marker, GeneratedAgentPublicationManagedCancellation)
    assert marker.claim_managed is True
    if not marker.claim_managed:  # Mirrors lifecycle's caller-owned cleanup.
        journal.draft.generation_claim_id = None
    assert journal.draft.generation_claim_id == CLAIM
    assert attempt.waiters == 1

    admission_release.set()
    assert (await survivor).publication.state == "published"
    await service.close()


@pytest.mark.asyncio
async def test_late_joiner_never_attaches_to_zero_waiter_cancelling_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeStore()
    store.stage_release.clear()
    service, _journal, _store, _runtime, _admission = _service(store=store)
    request = _request()
    cancelled = asyncio.create_task(service.publish(request))
    assert await asyncio.to_thread(store.stage_entered.wait, 1)
    old_attempt = next(iter(service._attempts.values()))
    original_publish_attempt = service._publish_attempt
    late_attempts: list[Any] = []

    async def track_late_attempt(attempt: Any):
        late_attempts.append(attempt)
        return await original_publish_attempt(attempt)

    monkeypatch.setattr(service, "_publish_attempt", track_late_attempt)

    cancelled.cancel()
    await asyncio.sleep(0)
    assert old_attempt.accepting_waiters is False
    late = asyncio.create_task(service.publish(request))
    for _ in range(100):
        if late_attempts or late.done():
            break
        await asyncio.sleep(0.001)
    assert late_attempts and late_attempts[0] is not old_attempt
    assert not late.cancelled()

    store.stage_release.set()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    with pytest.raises(GeneratedAgentPublicationRecoveryPendingError):
        await late
    await service.close()


@pytest.mark.asyncio
async def test_separate_service_observes_live_durable_intent_as_managed() -> None:
    store = _FakeStore()
    store.stage_release.clear()
    first, journal, _store, runtime, admission = _service(store=store)
    running = asyncio.create_task(first.publish(_request()))
    assert await asyncio.to_thread(store.stage_entered.wait, 1)
    repositories = SimpleNamespace(
        generated_agent_publications=journal,
        agents=_FakeAgents(journal),
        draft_agents=_FakeDrafts(journal),
    )
    second = GeneratedAgentPublicationService(
        plane_runtime=runtime,
        plane_repositories=repositories,
        bundle_store=store,
        work_admission=admission,
    )
    cross_replica_readiness = await second.readiness()
    assert cross_replica_readiness.ready is True
    assert cross_replica_readiness.ignored_live_count == 1
    with pytest.raises(GeneratedAgentPublicationRecoveryPendingError) as captured:
        await second.publish(_request())
    assert captured.value.claim_managed is True
    assert journal.publication is not None and journal.publication.state == "claimed"
    store.stage_release.set()
    assert (await running).publication.state == "published"
    await second.close()
    await first.close()


def _seed_recovery(journal: _FakeJournal, *, state: str) -> None:
    request = _request()
    fake_attempt = SimpleNamespace(
        operation_id=uuid.uuid4(),
        execution_generation=1,
    )
    canonical = generated_agent_publication_identity(
        owner_id=OWNER,
        draft_uuid=DRAFT,
        source_state_revision=7,
        generation_claim_id=CLAIM,
        target_agent_id=AGENT,
    )
    canonical_paths = generated_agent_publication_paths(
        draft_uuid=DRAFT,
        source_state_revision=7,
        publication_id=str(canonical.publication_id),
        target_agent_id=AGENT,
        target_revision_id=str(canonical.target_revision_id),
    )
    intent = journal.begin_intent(
        object(),
        owner_id=OWNER,
        publication_id=str(canonical.publication_id),
        draft_uuid=DRAFT,
        source_state_revision=7,
        generation_claim_id=CLAIM,
        target_agent_id=AGENT,
        target_revision_id=str(canonical.target_revision_id),
        staging_relative_path=canonical_paths.staging_relative_path,
        revision_relative_path=canonical_paths.revision_relative_path,
        bundle=request.bundle,
        runtime_contract_version=3,
        release_lock_digest=LOCK_DIGEST,
        promotion_token=str(canonical.promotion_token),
        attempt=fake_attempt,
        compatibility_state="compatible",
    )
    journal.publication = replace(intent.publication, state=state)
    if state == "validated":
        journal.publication = replace(
            journal.publication,
            artifact_digest=request.bundle.bundle_sha256,
            manifest_digest=hashlib.sha256(
                request.bundle.manifest_json.encode()
            ).hexdigest(),
        )
        for field_name in (
            "error_message",
            "security_report",
            "validation_report",
            "required_credentials",
        ):
            setattr(journal.draft, field_name, getattr(RESULT_METADATA, field_name))


@pytest.mark.asyncio
async def test_recovery_binding_claim_commit_ack_loss_reselects_before_rebind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _FakeJournal()
    _seed_recovery(journal, state="validated")
    store = _FakeStore()
    store.recovery = BundleRecoveryResult(BundleRecoveryDisposition.ABSENT)
    service, _journal, _store, _runtime, admission = _service(
        journal=journal,
        store=store,
    )
    original_claim = admission.claim_operation
    ambiguous_claims: list[Any] = []

    def commit_then_lose_ack(*args: Any, **kwargs: Any):
        claim = original_claim(*args, **kwargs)
        assert claim is not None
        ambiguous_claims.append(claim)
        raise RuntimeError("recovery claim acknowledgement lost")

    monkeypatch.setattr(admission, "claim_operation", commit_then_lose_ack)

    report = await service.recover_once()

    assert report.failed == 1
    assert len(ambiguous_claims) == 1
    ambiguous = ambiguous_claims[0]
    assert journal.publication is not None
    assert journal.publication.operation_id == str(ambiguous.fence.operation_id)
    assert (
        journal.publication.operation_execution_generation
        == ambiguous.fence.execution_generation + 1
    )
    assert journal.events.count("begin") == 1
    assert journal.events.count("rebind") == 1
    operation = admission.repository.get_operation_for_administration(
        ambiguous.fence.operation_id
    )
    assert operation is not None and operation.state is OperationState.FAILED
    assert (
        operation.execution_generation
        == journal.publication.operation_execution_generation
    )
    await service.close()


def _seed_expired_prejournal_claim(journal: _FakeJournal) -> None:
    journal.expired_claims = (
        SimpleNamespace(
            draft_id="expired-prejournal-draft-row",
            owner_id=OWNER,
            status="generating",
            draft_uuid=DRAFT,
            target_agent_id=AGENT,
            state_revision=7,
            generation_claim_id=CLAIM,
            generation_claim_expires_at=datetime(2026, 8, 14, tzinfo=UTC),
            published_revision_id=None,
            error_message=None,
            security_report=None,
            validation_report=None,
            required_credentials=None,
        ),
    )


@pytest.mark.asyncio
async def test_expired_prejournal_claim_blocks_readiness_then_fails_exact_claim() -> (
    None
):
    journal = _FakeJournal()
    _seed_expired_prejournal_claim(journal)
    service, _journal, _store, _runtime, _admission = _service(journal=journal)

    before = await service.readiness()
    report = await service.recover_once()
    after = await service.readiness()

    assert before.ready is False
    assert before.unresolved_count == 1
    assert before.unresolved_publication_ids[0].startswith("prejournal:")
    assert report.inspected == 1
    assert report.failed == 1
    assert report.degraded_publication_ids == ()
    assert journal.reclaimed_expired_claim is not None
    assert journal.finished_expired_claim is not None
    assert journal.finished_expired_claim.status == "error"
    assert journal.finished_expired_claim.generation_claim_id is None
    assert after.ready is True
    await service.close()


@pytest.mark.asyncio
async def test_expired_claim_cleanup_joins_db_worker_before_rethrowing_cancellation() -> (
    None
):
    journal = _FakeJournal()
    _seed_expired_prejournal_claim(journal)
    journal.expired_finish_release.clear()
    service, _journal, _store, _runtime, _admission = _service(journal=journal)
    recovery = asyncio.create_task(service.recover_once())
    assert await asyncio.to_thread(journal.expired_finish_entered.wait, 1)

    recovery.cancel()
    journal.expired_finish_release.set()
    with pytest.raises(asyncio.CancelledError):
        await recovery

    assert journal.finished_expired_claim is not None
    assert journal.finished_expired_claim.generation_claim_id is None
    await service.close()


@pytest.mark.asyncio
async def test_expired_claim_journal_race_rolls_back_to_journal_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _FakeJournal()
    _seed_expired_prejournal_claim(journal)
    expired_claim = journal.expired_claims[0]
    _seed_recovery(journal, state="claimed")
    service, _journal, _store, _runtime, _admission = _service(journal=journal)
    monkeypatch.setattr(
        service,
        "_list_reconcilable",
        lambda *, limit, after=None: (),
    )
    monkeypatch.setattr(
        service,
        "_expired_claim_inventory",
        lambda *, limit, after=None: SimpleNamespace(
            claims=(expired_claim,),
            raw_count=1,
            next_cursor=(
                expired_claim.generation_claim_expires_at,
                expired_claim.draft_id,
            ),
            has_more=False,
        ),
    )

    report = await service.recover_once()

    assert report.failed == 0
    assert len(report.degraded_publication_ids) == 1
    assert report.degraded_publication_ids[0].startswith("prejournal:")
    assert journal.reclaimed_expired_claim is not None
    assert journal.finished_expired_claim is None
    assert journal.publication is not None and journal.publication.state == "claimed"
    await service.close()


@pytest.mark.asyncio
async def test_recovery_gives_journal_and_prejournal_inventories_independent_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _FakeJournal()
    _seed_recovery(journal, state="claimed")
    _seed_expired_prejournal_claim(journal)
    claim = journal.expired_claims[0]
    journal.expired_claims = (
        SimpleNamespace(
            **{
                **vars(claim),
                "draft_uuid": "50000000-0000-4000-8000-000000000074",
                "generation_claim_id": "60000000-0000-4000-8000-000000000074",
            }
        ),
    )
    service, _journal, _store, _runtime, _admission = _service(
        journal=journal,
        recovery_batch_size=1,
    )

    async def journal_is_live(_publication: Any) -> bool:
        return True

    monkeypatch.setattr(service, "_is_live_attempt", journal_is_live)
    report = await service.recover_once()

    assert report.inspected == 2
    assert report.skipped_live == 1
    assert report.failed == 1
    assert journal.finished_expired_claim is not None
    await service.close()


@pytest.mark.asyncio
async def test_readiness_overflow_sentinel_prevents_live_first_page_false_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _FakeJournal()
    _seed_recovery(journal, state="claimed")
    assert journal.publication is not None
    second = replace(journal.publication, publication_id=str(uuid.uuid4()))
    service, _journal, _store, _runtime, _admission = _service(
        journal=journal,
        recovery_batch_size=1,
    )
    monkeypatch.setattr(
        service,
        "_list_reconcilable",
        lambda *, limit, after=None: (journal.publication, second)[:limit],
    )

    async def journal_is_live(_publication: Any) -> bool:
        return True

    monkeypatch.setattr(service, "_is_live_attempt", journal_is_live)
    readiness = await service.readiness()

    assert readiness.ready is False
    assert readiness.ignored_live_count == 1
    assert readiness.unresolved_publication_ids == ("inventory:journal-overflow",)
    await service.close()


@pytest.mark.asyncio
async def test_recovery_keyset_traverses_more_than_one_thousand_journal_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _journal, _store, _runtime, _admission = _service(
        recovery_batch_size=400,
    )
    base = datetime(2026, 8, 15, tzinfo=UTC)
    publications = tuple(
        SimpleNamespace(
            created_at=base + timedelta(microseconds=index),
            publication_id=str(uuid.UUID(int=index + 1, version=4)),
        )
        for index in range(1_005)
    )
    inspected: list[str] = []

    def paged_journal(
        *,
        limit: int,
        after: tuple[datetime, str] | None = None,
    ):
        rows = publications
        if after is not None:
            rows = tuple(
                publication
                for publication in rows
                if (publication.created_at, publication.publication_id) > after
            )
        return rows[:limit]

    async def live(publication: Any) -> bool:
        inspected.append(publication.publication_id)
        return True

    monkeypatch.setattr(service, "_list_reconcilable", paged_journal)
    monkeypatch.setattr(service, "_is_live_attempt", live)
    reports = [await service.recover_once() for _ in range(3)]

    assert [report.inspected for report in reports] == [400, 400, 205]
    assert reports[0].degraded_publication_ids == ("inventory:journal-overflow",)
    assert reports[1].degraded_publication_ids == ("inventory:journal-overflow",)
    assert reports[2].degraded_publication_ids == ()
    assert inspected == [row.publication_id for row in publications]
    await service.close()


@pytest.mark.asyncio
async def test_recovery_keyset_traverses_more_than_one_thousand_expired_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _FakeJournal()
    base = datetime(2026, 8, 15, tzinfo=UTC)
    journal.expired_claims = tuple(
        SimpleNamespace(
            draft_id=f"expired-draft-{index:04d}",
            owner_id=OWNER,
            draft_uuid=f"expired-draft-uuid-{index:04d}",
            state_revision=7,
            generation_claim_id=str(uuid.UUID(int=index + 1, version=4)),
            generation_claim_expires_at=base + timedelta(microseconds=index),
        )
        for index in range(1_005)
    )
    service, _journal, _store, _runtime, _admission = _service(
        journal=journal,
        recovery_batch_size=400,
    )
    terminalized: list[str] = []

    def terminalize(claim: Any) -> None:
        terminalized.append(claim.draft_id)

    monkeypatch.setattr(service, "_terminalize_expired_claim", terminalize)
    reports = [await service.recover_once() for _ in range(3)]

    assert [report.inspected for report in reports] == [400, 400, 205]
    assert reports[0].degraded_publication_ids == ("inventory:prejournal-overflow",)
    assert reports[1].degraded_publication_ids == ("inventory:prejournal-overflow",)
    assert reports[2].degraded_publication_ids == ()
    assert terminalized == [claim.draft_id for claim in journal.expired_claims]
    await service.close()


@pytest.mark.parametrize(
    "disposition",
    (
        BundleRecoveryDisposition.FINAL_VALID,
        BundleRecoveryDisposition.STAGING_PROMOTED,
    ),
)
@pytest.mark.asyncio
async def test_validated_recovery_commits_only_persisted_result_metadata(
    disposition: BundleRecoveryDisposition,
) -> None:
    journal = _FakeJournal()
    _seed_recovery(journal, state="validated")
    store = _FakeStore()
    request = _request()
    identity = _identity()
    key = BundlePublicationKey(
        scope_id=AGENT,
        staging_id=DRAFT,
        source_revision=7,
        publication_id=str(identity.publication_id),
        revision_id=str(identity.target_revision_id),
    )
    receipt = StagedBundleReceipt(
        paths=paths_for(key),
        publication_key=key,
        storage_identity=object(),  # type: ignore[arg-type]
        bundle_sha256=request.bundle.bundle_sha256,
        manifest_sha256=hashlib.sha256(
            request.bundle.manifest_json.encode()
        ).hexdigest(),
        runtime_metadata=request.bundle.runtime_metadata,
    )
    published = store._published(receipt)
    store.published = published
    store.recovery = BundleRecoveryResult(disposition, published=published)
    service, _journal, _store, _runtime, _admission = _service(
        journal=journal,
        store=store,
    )

    report = await service.recover_once()

    assert report.recovered == 1
    assert report.degraded_publication_ids == ()
    assert journal.publication is not None and journal.publication.state == "published"
    assert journal.draft.security_report == RESULT_METADATA.security_report
    await service.close()


@pytest.mark.asyncio
async def test_recovery_commit_state_then_ack_error_reconciles_completed_operation() -> (
    None
):
    journal = _FakeJournal()
    _seed_recovery(journal, state="validated")
    journal.commit_after_state_error = RuntimeError("recovery commit ack lost")
    store = _FakeStore()
    request = _request()
    identity = _identity()
    key = BundlePublicationKey(
        scope_id=AGENT,
        staging_id=DRAFT,
        source_revision=7,
        publication_id=str(identity.publication_id),
        revision_id=str(identity.target_revision_id),
    )
    receipt = StagedBundleReceipt(
        paths=paths_for(key),
        publication_key=key,
        storage_identity=object(),  # type: ignore[arg-type]
        bundle_sha256=request.bundle.bundle_sha256,
        manifest_sha256=hashlib.sha256(
            request.bundle.manifest_json.encode()
        ).hexdigest(),
        runtime_metadata=request.bundle.runtime_metadata,
    )
    published = store._published(receipt)
    store.recovery = BundleRecoveryResult(
        BundleRecoveryDisposition.FINAL_VALID,
        published=published,
    )
    service, _journal, _store, _runtime, admission = _service(
        journal=journal,
        store=store,
    )

    report = await service.recover_once()

    assert report.recovered == 1
    assert journal.publication is not None
    assert journal.publication.state == "published"
    assert journal.publication.operation_id is not None
    operation = admission.repository.get_operation_for_administration(
        uuid.UUID(journal.publication.operation_id)
    )
    assert operation is not None and operation.state is OperationState.COMPLETED
    await service.close()


@pytest.mark.parametrize(
    ("terminal_state", "operation_state"),
    (
        ("published", OperationState.COMPLETED),
        ("failed", OperationState.FAILED),
    ),
)
@pytest.mark.asyncio
async def test_recovery_cancellation_after_terminal_commit_finishes_operation(
    terminal_state: str,
    operation_state: OperationState,
) -> None:
    journal = _FakeJournal()
    _seed_recovery(journal, state="validated")
    store = _FakeStore()
    if terminal_state == "published":
        request = _request()
        identity = _identity()
        key = BundlePublicationKey(
            scope_id=AGENT,
            staging_id=DRAFT,
            source_revision=7,
            publication_id=str(identity.publication_id),
            revision_id=str(identity.target_revision_id),
        )
        receipt = StagedBundleReceipt(
            paths=paths_for(key),
            publication_key=key,
            storage_identity=object(),  # type: ignore[arg-type]
            bundle_sha256=request.bundle.bundle_sha256,
            manifest_sha256=hashlib.sha256(
                request.bundle.manifest_json.encode()
            ).hexdigest(),
            runtime_metadata=request.bundle.runtime_metadata,
        )
        published = store._published(receipt)
        store.recovery = BundleRecoveryResult(
            BundleRecoveryDisposition.FINAL_VALID,
            published=published,
        )
        barrier_entered = journal.commit_entered
        barrier_release = journal.commit_release
    else:
        store.recovery = BundleRecoveryResult(BundleRecoveryDisposition.ABSENT)
        barrier_entered = journal.fail_entered
        barrier_release = journal.fail_release
    barrier_release.clear()
    service, _journal, _store, _runtime, admission = _service(
        journal=journal,
        store=store,
    )
    task = asyncio.create_task(service.recover_once())
    assert await asyncio.to_thread(barrier_entered.wait, 1)

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    barrier_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert journal.publication is not None
    assert journal.publication.state == terminal_state
    assert journal.publication.operation_id is not None
    operation = admission.repository.get_operation_for_administration(
        uuid.UUID(journal.publication.operation_id)
    )
    assert operation is not None and operation.state is operation_state
    await service.close()


@pytest.mark.parametrize(
    "disposition",
    (BundleRecoveryDisposition.ABSENT, BundleRecoveryDisposition.PARTIAL),
)
@pytest.mark.asyncio
async def test_incomplete_validated_recovery_fails_without_inventing_reports(
    disposition: BundleRecoveryDisposition,
) -> None:
    journal = _FakeJournal()
    _seed_recovery(journal, state="validated")
    store = _FakeStore()
    store.recovery = BundleRecoveryResult(
        disposition, quarantined=disposition.value == "partial"
    )
    service, _journal, _store, _runtime, _admission = _service(
        journal=journal,
        store=store,
    )
    report = await service.recover_once()
    assert report.failed == 1
    assert journal.publication is not None and journal.publication.state == "failed"
    await service.close()


@pytest.mark.asyncio
async def test_recovery_failure_state_then_ack_error_reconciles_failed_operation() -> (
    None
):
    journal = _FakeJournal()
    _seed_recovery(journal, state="validated")
    journal.fail_after_state_error = RuntimeError("recovery failure ack lost")
    store = _FakeStore()
    store.recovery = BundleRecoveryResult(BundleRecoveryDisposition.ABSENT)
    service, _journal, _store, _runtime, admission = _service(
        journal=journal,
        store=store,
    )

    report = await service.recover_once()

    assert report.failed == 1
    assert journal.publication is not None
    assert journal.publication.state == "failed"
    assert journal.publication.operation_id is not None
    operation = admission.repository.get_operation_for_administration(
        uuid.UUID(journal.publication.operation_id)
    )
    assert operation is not None and operation.state is OperationState.FAILED
    await service.close()


@pytest.mark.asyncio
async def test_recovery_failure_repairs_operation_terminalization_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _FakeJournal()
    _seed_recovery(journal, state="validated")
    store = _FakeStore()
    store.recovery = BundleRecoveryResult(BundleRecoveryDisposition.ABSENT)
    service, _journal, _store, runtime, admission = _service(
        journal=journal,
        store=store,
    )
    original_terminalize = admission.terminalize
    terminal_transactions: list[object] = []

    def fail_first_terminalize(*args: Any, **kwargs: Any):
        terminal_transactions.append(kwargs["transaction"])
        if len(terminal_transactions) == 1:
            raise RuntimeError("recovery operation terminalization unavailable")
        return original_terminalize(*args, **kwargs)

    monkeypatch.setattr(admission, "terminalize", fail_first_terminalize)
    report = await service.recover_once()

    assert report.failed == 1
    assert journal.publication is not None and journal.publication.state == "failed"
    assert terminal_transactions[0] is journal.terminal_transactions[-1]
    assert all(transaction in runtime.transactions for transaction in terminal_transactions)
    assert journal.publication.operation_id is not None
    operation = admission.repository.get_operation_for_administration(
        uuid.UUID(journal.publication.operation_id)
    )
    assert operation is not None and operation.state is OperationState.FAILED
    assert (await service.readiness()).ready is True
    await service.close()


@pytest.mark.asyncio
async def test_cancellation_during_recovery_quarantine_stays_primary() -> None:
    journal = _FakeJournal()
    _seed_recovery(journal, state="claimed")
    store = _FakeStore()
    request = _request()
    identity = _identity()
    key = BundlePublicationKey(
        scope_id=AGENT,
        staging_id=DRAFT,
        source_revision=7,
        publication_id=str(identity.publication_id),
        revision_id=str(identity.target_revision_id),
    )
    receipt = StagedBundleReceipt(
        paths=paths_for(key),
        publication_key=key,
        storage_identity=object(),  # type: ignore[arg-type]
        bundle_sha256=request.bundle.bundle_sha256,
        manifest_sha256=hashlib.sha256(
            request.bundle.manifest_json.encode()
        ).hexdigest(),
        runtime_metadata=request.bundle.runtime_metadata,
    )
    published = store._published(receipt)
    store.recovery = BundleRecoveryResult(
        BundleRecoveryDisposition.FINAL_VALID,
        published=published,
    )
    store.quarantine_receipt_release.clear()
    service, _journal, _store, _runtime, _admission = _service(
        journal=journal,
        store=store,
    )
    task = asyncio.create_task(service.recover_once())
    assert await asyncio.to_thread(store.quarantine_receipt_entered.wait, 1)

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    store.quarantine_receipt_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.quarantined_published == 1
    assert journal.publication is not None and journal.publication.state == "claimed"
    await service.close()


@pytest.mark.parametrize(
    "disposition",
    (BundleRecoveryDisposition.FOREIGN, BundleRecoveryDisposition.COLLISION),
)
@pytest.mark.asyncio
async def test_foreign_recovery_remains_degraded_and_operator_visible(
    disposition: BundleRecoveryDisposition,
) -> None:
    journal = _FakeJournal()
    _seed_recovery(journal, state="validated")
    store = _FakeStore()
    store.recovery = BundleRecoveryResult(disposition, detail="foreign bytes")
    service, _journal, _store, _runtime, _admission = _service(
        journal=journal,
        store=store,
    )
    report = await service.recover_once()
    readiness = await service.readiness()
    assert report.degraded_publication_ids == (journal.publication.publication_id,)
    assert readiness.ready is False
    assert readiness.unresolved_count == 1
    await service.close()


@pytest.mark.asyncio
async def test_claimed_recovery_quarantines_exact_promoted_bytes_then_fails() -> None:
    journal = _FakeJournal()
    _seed_recovery(journal, state="claimed")
    store = _FakeStore()
    request = _request()
    identity = _identity()
    key = BundlePublicationKey(
        scope_id=AGENT,
        staging_id=DRAFT,
        source_revision=7,
        publication_id=str(identity.publication_id),
        revision_id=str(identity.target_revision_id),
    )
    staged = StagedBundleReceipt(
        paths=paths_for(key),
        publication_key=key,
        storage_identity=object(),  # type: ignore[arg-type]
        bundle_sha256=request.bundle.bundle_sha256,
        manifest_sha256=hashlib.sha256(
            request.bundle.manifest_json.encode()
        ).hexdigest(),
        runtime_metadata=request.bundle.runtime_metadata,
    )
    published = store._published(staged)
    store.recovery = BundleRecoveryResult(
        BundleRecoveryDisposition.STAGING_PROMOTED,
        published=published,
    )
    service, _journal, _store, _runtime, _admission = _service(
        journal=journal,
        store=store,
    )
    report = await service.recover_once()
    assert report.failed == 1
    assert store.quarantined_published == 1
    assert journal.publication is not None and journal.publication.state == "failed"
    await service.close()


@pytest.mark.parametrize("failure", ("missing_revision", "rebind", "recover"))
@pytest.mark.asyncio
async def test_recovery_internal_failure_stays_degraded_for_retry(failure: str) -> None:
    journal = _FakeJournal()
    _seed_recovery(journal, state="validated")
    store = _FakeStore()
    store.recovery = BundleRecoveryResult(BundleRecoveryDisposition.ABSENT)
    if failure == "missing_revision":
        journal.missing_revision = True
    elif failure == "rebind":
        journal.rebind_error = RuntimeError("rebind failed")
    else:
        store.recover_error = RuntimeError("inspection failed")
    service, _journal, _store, _runtime, _admission = _service(
        journal=journal, store=store
    )
    report = await service.recover_once()
    assert report.degraded_publication_ids == (journal.publication.publication_id,)
    assert journal.publication.state == "validated"
    await service.close()


@pytest.mark.parametrize("failure", ("quarantine", "journal_fail"))
@pytest.mark.asyncio
async def test_claimed_recovery_cleanup_failure_never_reports_terminal_success(
    failure: str,
) -> None:
    journal = _FakeJournal()
    _seed_recovery(journal, state="claimed")
    store = _FakeStore()
    request = _request()
    identity = _identity()
    key = BundlePublicationKey(
        scope_id=AGENT,
        staging_id=DRAFT,
        source_revision=7,
        publication_id=str(identity.publication_id),
        revision_id=str(identity.target_revision_id),
    )
    staged = StagedBundleReceipt(
        paths=paths_for(key),
        publication_key=key,
        storage_identity=object(),  # type: ignore[arg-type]
        bundle_sha256=request.bundle.bundle_sha256,
        manifest_sha256=hashlib.sha256(
            request.bundle.manifest_json.encode()
        ).hexdigest(),
        runtime_metadata=request.bundle.runtime_metadata,
    )
    published = store._published(staged)
    store.recovery = BundleRecoveryResult(
        BundleRecoveryDisposition.FINAL_VALID,
        published=published,
    )
    if failure == "quarantine":
        store.quarantine_receipt_error = RuntimeError("quarantine failed")
    else:
        journal.fail_error = RuntimeError("fail transition failed")
    service, _journal, _store, _runtime, _admission = _service(
        journal=journal, store=store
    )
    report = await service.recover_once()
    assert report.degraded_publication_ids == (journal.publication.publication_id,)
    assert journal.publication.state == "claimed"
    await service.close()


@pytest.mark.asyncio
async def test_recovery_is_single_flight_and_cancellation_terminalizes_child() -> None:
    journal = _FakeJournal()
    _seed_recovery(journal, state="validated")
    store = _FakeStore()
    store.recovery = BundleRecoveryResult(BundleRecoveryDisposition.ABSENT)
    store.recover_release.clear()
    service, _journal, _store, _runtime, _admission = _service(
        journal=journal, store=store
    )
    active = asyncio.create_task(service.recover_once())
    assert await asyncio.to_thread(store.recover_entered.wait, 1)
    concurrent = await service.recover_once()
    assert concurrent.inspected == 0
    active.cancel()
    store.recover_release.set()
    with pytest.raises(asyncio.CancelledError):
        await active
    assert journal.publication is not None and journal.publication.state == "validated"
    await service.close()


@pytest.mark.asyncio
async def test_recovery_cancellation_joins_claim_and_terminalizes_child_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _FakeJournal()
    _seed_recovery(journal, state="claimed")
    store = _FakeStore()
    store.recovery = BundleRecoveryResult(BundleRecoveryDisposition.ABSENT)
    service, _journal, _store, _runtime, admission = _service(
        journal=journal,
        store=store,
    )
    entered = threading.Event()
    release = threading.Event()
    claimed: list[Any] = []
    original_claim = admission.claim_operation

    def blocked_claim(*args: Any, **kwargs: Any):
        result = original_claim(*args, **kwargs)
        claimed.append(result)
        entered.set()
        release.wait(2)
        return result

    monkeypatch.setattr(admission, "claim_operation", blocked_claim)
    task = asyncio.create_task(service.recover_once())
    assert await asyncio.to_thread(entered.wait, 1)

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(claimed) == 1
    operation = admission.repository.get_operation_for_administration(
        claimed[0].fence.operation_id
    )
    assert operation is not None and operation.state is OperationState.CANCELLED
    assert "rebind" not in journal.events
    await service.close()


@pytest.mark.asyncio
async def test_recovery_cancellation_after_rebind_keeps_journal_recoverable() -> None:
    journal = _FakeJournal()
    _seed_recovery(journal, state="claimed")
    journal.rebind_release.clear()
    store = _FakeStore()
    store.recovery = BundleRecoveryResult(BundleRecoveryDisposition.ABSENT)
    service, _journal, _store, _runtime, admission = _service(
        journal=journal,
        store=store,
    )
    task = asyncio.create_task(service.recover_once())
    assert await asyncio.to_thread(journal.rebind_entered.wait, 1)

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    journal.rebind_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert journal.publication is not None
    assert journal.publication.state == "claimed"
    assert journal.publication.operation_id is not None
    operation = admission.repository.get_operation_for_administration(
        uuid.UUID(journal.publication.operation_id)
    )
    assert operation is not None and operation.state is OperationState.RETRYABLE
    await service.close()


@pytest.mark.asyncio
async def test_recovery_and_snapshot_defensive_boundaries_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _FakeJournal()
    _seed_recovery(journal, state="validated")
    store = _FakeStore()
    store.recovery = BundleRecoveryResult(BundleRecoveryDisposition.ABSENT)
    service, _journal, _store, _runtime, _admission = _service(
        journal=journal, store=store
    )

    async def refused_recovery(**_values: Any):
        raise GeneratedAgentPublicationPreIntentError("no recovery capacity")

    monkeypatch.setattr(service, "_admit_and_claim", refused_recovery)
    report = await service.recover_once()
    assert report.degraded_publication_ids == (journal.publication.publication_id,)

    await service._recovery_loop()
    assert (
        service._terminalize_operation_sync(
            SimpleNamespace(deep_fence=None),
            state=OperationState.FAILED,
            terminal_code="defensive_test",
            safe_summary=None,
            retry_after_ms=None,
        )
        is None
    )
    marker_error = RuntimeError("unmanaged")
    unmanaged = SimpleNamespace(
        snapshot_lock=threading.RLock(),
        publication=None,
    )
    assert service._cancellation_marker(unmanaged, marker_error) is marker_error
    remembered = asyncio.CancelledError()
    heartbeat_outcome = SimpleNamespace(cancellation=remembered, error=None)
    assert service._merge_cleanup_error(heartbeat_outcome, None) is remembered
    later = RuntimeError("later cleanup failure")
    assert service._merge_cleanup_error(heartbeat_outcome, later) is remembered
    assert remembered.__cause__ is later
    await service.close()


@pytest.mark.asyncio
async def test_recovery_cursor_and_authority_defensive_edges_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _journal, _store, _runtime, _admission = _service()
    now = datetime(2026, 8, 15, tzinfo=UTC)

    with pytest.raises(GeneratedAgentPublicationRecoveryPendingError):
        service._publication_cursor(
            SimpleNamespace(created_at="not-a-time", publication_id=str(uuid.uuid4()))
        )
    with pytest.raises(GeneratedAgentPublicationRecoveryPendingError):
        service._publication_cursor(SimpleNamespace(created_at=now, publication_id=""))
    with pytest.raises(GeneratedAgentPublicationRecoveryPendingError):
        service._expired_claim_cursor(
            SimpleNamespace(generation_claim_expires_at=None, draft_id="draft")
        )
    with pytest.raises(GeneratedAgentPublicationRecoveryPendingError):
        service._expired_claim_cursor(
            SimpleNamespace(generation_claim_expires_at=now, draft_id="")
        )
    assert service._expired_claim_inventory(limit=0).raw_count == 0
    with pytest.raises(GenerationClaimLostError):
        service._terminalize_expired_claim(SimpleNamespace(generation_claim_id=None))

    assert (
        await service._is_live_attempt(
            SimpleNamespace(operation_id=None, operation_execution_generation=None)
        )
        is False
    )
    assert (
        await service._is_live_attempt(
            SimpleNamespace(
                operation_id="not-a-uuid",
                operation_execution_generation=1,
            )
        )
        is False
    )

    journal_calls: list[tuple[datetime, str] | None] = []
    journal_item = object()

    def journal_page(*, limit: int, after: tuple[datetime, str] | None):
        journal_calls.append(after)
        if after is not None:
            return SimpleNamespace(publications=(), next_cursor=None, has_more=False)
        return SimpleNamespace(
            publications=(journal_item,),
            next_cursor=(now, str(uuid.uuid4())),
            has_more=False,
        )

    service._journal_recovery_cursor = (now, str(uuid.uuid4()))
    monkeypatch.setattr(service, "_publication_inventory", journal_page)
    assert service._next_journal_recovery_page().publications == (journal_item,)
    assert len(journal_calls) == 2 and journal_calls[1] is None

    expired_calls: list[tuple[datetime, str] | None] = []
    expired_item = object()

    def expired_page(*, limit: int, after: tuple[datetime, str] | None = None):
        expired_calls.append(after)
        if after is not None:
            return SimpleNamespace(
                claims=(),
                raw_count=0,
                next_cursor=None,
                has_more=False,
            )
        return SimpleNamespace(
            claims=(expired_item,),
            raw_count=1,
            next_cursor=(now, "draft"),
            has_more=False,
        )

    service._expired_claim_recovery_cursor = (now, "previous-draft")
    monkeypatch.setattr(service, "_expired_claim_inventory", expired_page)
    assert service._next_expired_claim_recovery_page().claims == (expired_item,)
    assert len(expired_calls) == 2 and expired_calls[1] is None
    await service.close()


@pytest.mark.asyncio
async def test_readiness_ignores_exact_live_attempt_and_close_joins_it() -> None:
    store = _FakeStore()
    store.stage_release.clear()
    service, journal, _store, _runtime, _admission = _service(store=store)
    task = asyncio.create_task(service.publish(_request()))
    assert await asyncio.to_thread(store.stage_entered.wait, 1)
    readiness = await service.readiness()
    assert readiness.ready is True
    assert readiness.ignored_live_count == 1

    close_task = asyncio.create_task(service.close())
    await asyncio.sleep(0)
    store.stage_release.set()
    await close_task
    with pytest.raises(asyncio.CancelledError):
        await task
    assert journal.publication is not None and journal.publication.state == "failed"


@pytest.mark.asyncio
async def test_live_authority_check_joins_worker_before_propagating_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _FakeJournal()
    _seed_recovery(journal, state="claimed")
    assert journal.publication is not None
    service, _journal, _store, _runtime, admission = _service(journal=journal)
    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()

    def blocked_lookup(_operation_id: uuid.UUID):
        entered.set()
        release.wait(2)
        exited.set()
        return None

    monkeypatch.setattr(
        admission.repository,
        "get_operation_for_administration",
        blocked_lookup,
    )
    task = asyncio.create_task(service._is_live_attempt(journal.publication))
    assert await asyncio.to_thread(entered.wait, 1)

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert exited.is_set()
    await service.close()


@pytest.mark.asyncio
async def test_close_fences_late_recovery_and_joins_retained_recovery_task() -> None:
    service, _journal, _store, _runtime, _admission = _service()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def retained_recovery() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    retained = asyncio.create_task(retained_recovery())
    await entered.wait()
    service._recovery_task = retained
    service._started = True
    close_task = asyncio.create_task(service.close())
    await asyncio.sleep(0)

    late = await service.recover_once()
    assert late.inspected == 0
    assert close_task.done() is False

    release.set()
    await close_task
    await service.close()


@pytest.mark.asyncio
async def test_expired_durable_lease_is_unready_and_recovered_while_task_is_local() -> (
    None
):
    current_time = [datetime(2026, 8, 15, tzinfo=UTC)]
    admission = _coordinator(
        clock=lambda: current_time[0],
        slot_lease=timedelta(seconds=1),
    )
    store = _FakeStore()
    store.stage_release.clear()
    store.recovery = BundleRecoveryResult(BundleRecoveryDisposition.ABSENT)
    service, journal, _store, _runtime, _admission = _service(
        store=store,
        admission=admission,
    )
    task = asyncio.create_task(service.publish(_request()))
    assert await asyncio.to_thread(store.stage_entered.wait, 1)

    current_time[0] += timedelta(seconds=2)
    readiness = await service.readiness()
    assert readiness.ready is False
    assert readiness.unresolved_count == 1
    assert readiness.ignored_live_count == 0

    report = await service.recover_once()
    assert report.failed == 1
    assert journal.publication is not None and journal.publication.state == "failed"

    store.stage_release.set()
    with pytest.raises(GeneratedAgentPublicationManagedError):
        await task
    assert store.quarantined_staged == 1
    await service.close()


@pytest.mark.asyncio
async def test_blocked_plane_transition_never_holds_snapshot_lock_on_event_loop() -> (
    None
):
    journal = _FakeJournal()
    journal.mark_staged_release.clear()
    service, _journal, _store, _runtime, _admission = _service(journal=journal)
    task = asyncio.create_task(service.publish(_request()))
    assert await asyncio.to_thread(journal.mark_staged_entered.wait, 1)
    attempt = next(iter(service._attempts.values()))

    safety_release = threading.Timer(1, journal.mark_staged_release.set)
    safety_release.start()
    started = asyncio.get_running_loop().time()
    marker = service._cancellation_marker(attempt, None)
    elapsed = asyncio.get_running_loop().time() - started
    journal.mark_staged_release.set()
    safety_release.cancel()

    assert isinstance(marker, GeneratedAgentPublicationManagedCancellation)
    assert elapsed < 0.1
    assert (await task).publication.state == "published"
    await service.close()


@pytest.mark.asyncio
async def test_generation_claim_heartbeat_renews_off_loop_and_joins() -> None:
    caller_thread = threading.get_ident()
    renewed = threading.Event()
    worker_threads: list[int] = []

    def renew():
        worker_threads.append(threading.get_ident())
        renewed.set()
        return object()

    heartbeat = GenerationClaimHeartbeat(
        renew,
        interval_seconds=0.01,
        task_name="test-generation-claim-renewal",
    )
    heartbeat.start()
    assert await asyncio.to_thread(renewed.wait, 1)
    heartbeat.assert_healthy()
    await heartbeat.close()

    assert worker_threads
    assert set(worker_threads) != {caller_thread}


@pytest.mark.asyncio
async def test_generation_claim_heartbeat_surfaces_stale_claim() -> None:
    heartbeat = GenerationClaimHeartbeat(
        lambda: None,
        interval_seconds=0.01,
        task_name="test-generation-claim-loss",
    )
    heartbeat.start()
    for _ in range(100):
        await asyncio.sleep(0.01)
        try:
            heartbeat.assert_healthy()
        except GenerationClaimLostError:
            break
    else:  # pragma: no cover - bounded diagnostic guard.
        pytest.fail("claim-loss heartbeat did not fail")
    with pytest.raises(GenerationClaimLostError, match="renewal failed"):
        await heartbeat.close()


@pytest.mark.asyncio
async def test_generation_claim_close_joins_worker_through_repeated_cancellation() -> (
    None
):
    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()

    def renew():
        entered.set()
        release.wait(timeout=2)
        exited.set()
        raise RuntimeError("synthetic renewal failure")

    heartbeat = GenerationClaimHeartbeat(
        renew,
        interval_seconds=0.01,
        task_name="test-generation-claim-cancelled-close",
    )
    heartbeat.start()
    assert await asyncio.to_thread(entered.wait, 1)

    close_task = asyncio.create_task(heartbeat.close())
    await asyncio.sleep(0)
    close_task.cancel()
    await asyncio.sleep(0)
    close_task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError) as captured:
        await close_task

    assert exited.is_set()
    assert isinstance(captured.value.__cause__, RuntimeError)
    assert str(captured.value.__cause__) == "synthetic renewal failure"


@pytest.mark.asyncio
async def test_service_heartbeat_join_propagates_cancellation_after_worker_exit() -> (
    None
):
    entered = threading.Event()
    release = threading.Event()

    def renew():
        entered.set()
        release.wait(timeout=2)
        raise RuntimeError("heartbeat worker failed")

    heartbeat = GenerationClaimHeartbeat(
        renew,
        interval_seconds=0.01,
        task_name="test-service-heartbeat-close-cancellation",
    )
    heartbeat.start()
    assert await asyncio.to_thread(entered.wait, 1)
    service, _journal, _store, _runtime, _admission = _service()

    close_task = asyncio.create_task(service._close_heartbeat_safely(heartbeat))
    await asyncio.sleep(0)
    close_task.cancel()
    await asyncio.sleep(0)
    close_task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError) as captured:
        await close_task

    assert isinstance(captured.value.__cause__, GenerationClaimLostError)
    assert isinstance(captured.value.__cause__.__cause__, RuntimeError)
    await service.close()
