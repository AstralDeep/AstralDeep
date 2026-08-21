"""Focused Plane-bound personal-agent lifecycle coverage for feature 074."""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import inspect
import json
import os
import textwrap
from types import SimpleNamespace
from typing import Iterator
import uuid

import psycopg2
from psycopg2 import sql
import pytest

from astralplane import (
    BLOB_LAYOUT_VERSION,
    CONTRACT_VERSION,
    MIGRATION_DIGEST,
    READ_COMPATIBLE_FROM,
    SCHEMA_REVISION,
)
from orchestrator.agent_generator import (
    BYO_BUNDLE_FILENAMES,
    BYO_RUNTIME_CONTRACT_VERSION,
    BYO_RUNTIME_LOCK_SHA256,
)
from orchestrator.agent_constitution import (
    AGENT_CONSTITUTION_VERSION,
    USER_AGENT_POLICY_REVISION,
)
from orchestrator.agent_lifecycle import (
    CandidateAgentMetadata,
    CandidatePreparation,
    PostgresPersonalAgentRevisionStore,
)
from orchestrator.orchestrator import Orchestrator
from orchestrator.plane_composition import (
    PlaneContractExpectation,
    compose_plane_runtime,
)
from orchestrator.user_agents import (
    PersonalAgentRuntimeRepository,
    RuntimeCompatibilityPolicy,
    StaleRuntimeGenerationError,
    UserAgentRegistry,
)
from orchestrator.work_admission import (
    AdmissionClass,
    OperationOwner,
    OperationRequest,
    OperationState,
    OwnerScope,
    WorkAdmissionCoordinator,
)


_DATABASE_ENV = "ASTRALPLANE_TEST_DATABASE_URL"
_POLICY = RuntimeCompatibilityPolicy(
    runtime_contract_version=BYO_RUNTIME_CONTRACT_VERSION,
    runtime_lock_sha256=BYO_RUNTIME_LOCK_SHA256,
)


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def test_production_runtime_repository_uses_only_application_plane() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(Orchestrator.__init__)))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _name(node.func) == "PersonalAgentRuntimeRepository"
    ]

    assert len(calls) == 1
    call = calls[0]
    assert call.args == []
    values = {keyword.arg: _name(keyword.value) for keyword in call.keywords}
    assert values["plane_runtime"] == "self.runtime_composition.plane.runtime"
    assert values["plane_repositories"] == (
        "self.runtime_composition.plane.repositories"
    )
    assert values["operation_repository"] == "self.work_admission.repository"


def test_legacy_database_runtime_injection_fails_closed() -> None:
    with pytest.raises(TypeError, match="database persistence injection is retired"):
        PersonalAgentRuntimeRepository(
            object(),
            compatibility_policy=_POLICY,
            operation_repository=object(),
        )


@pytest.fixture(scope="module")
def plane_boundary(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    base_url = os.environ.get(_DATABASE_ENV)
    if base_url is None:
        pytest.skip(f"{_DATABASE_ENV} is not configured")
    params = psycopg2.extensions.parse_dsn(base_url)
    database_name = f"astraldeep_agent_plane_{uuid.uuid4().hex}"
    admin = psycopg2.connect(**params)
    admin.autocommit = True
    try:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
            )
    except BaseException:
        admin.close()
        raise

    test_params = dict(params)
    test_params["dbname"] = database_name
    test_url = psycopg2.extensions.make_dsn(**test_params)
    composition = None
    try:
        composition = compose_plane_runtime(
            PlaneContractExpectation(
                contract_version=CONTRACT_VERSION,
                schema_revision=SCHEMA_REVISION,
                read_compatible_from=READ_COMPATIBLE_FROM,
                migration_sha256=MIGRATION_DIGEST,
                blob_layout_version=BLOB_LAYOUT_VERSION,
            ),
            database_url=test_url,
            blob_root=tmp_path_factory.mktemp("agent-plane-blobs"),
            personal_agent_artifact_root=tmp_path_factory.mktemp(
                "agent-plane-bundles"
            ),
            identity=f"deep-agent-test-{uuid.uuid4().hex[:12]}",
            minimum_connections=1,
            maximum_connections=2,
        )
        coordinator = WorkAdmissionCoordinator.from_plane(
            plane_runtime=composition.runtime,
            plane_repositories=composition.repositories,
        )
        yield SimpleNamespace(
            runtime=composition.runtime,
            repositories=composition.repositories,
            coordinator=coordinator,
        )
    finally:
        if composition is not None:
            composition.close()
        try:
            with admin.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (database_name,),
                )
                cursor.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {}").format(
                        sql.Identifier(database_name)
                    )
                )
        finally:
            admin.close()


def _running_operation(
    coordinator: WorkAdmissionCoordinator,
    *,
    owner_id: str,
    operation_kind: str,
):
    admitted = coordinator.submit(
        OperationRequest(
            operation_kind=operation_kind,
            admission_class=AdmissionClass.INTERACTIVE,
            owner=OperationOwner(OwnerScope.USER, owner_id, None),
            submission_id=uuid.uuid4(),
            idempotency_namespace=None,
            idempotency_key=None,
            normalized_input_digest=None,
            chat_id=None,
            parent_operation_id=None,
            connection_generation=None,
            request_generation=None,
        )
    )
    assert admitted.accepted
    claim = coordinator.claim_operation(
        AdmissionClass.INTERACTIVE,
        admitted.operation_id,
    )
    assert claim is not None
    return claim.fence


def _bound_runtime(
    plane_boundary: SimpleNamespace,
    *,
    online: bool,
) -> SimpleNamespace:
    owner_id = f"owner-{uuid.uuid4().hex}"
    agent_id = f"agent-{uuid.uuid4().hex}"
    registry = UserAgentRegistry(
        plane_runtime=plane_boundary.runtime,
        plane_repositories=plane_boundary.repositories,
    )
    registry.create(
        agent_id=agent_id,
        owner_user_id=owner_id,
        display_name="Physical-exit Plane agent",
    )
    registry.mark_validated(agent_id, "test-policy-v1")
    runtimes = PersonalAgentRuntimeRepository(
        compatibility_policy=_POLICY,
        operation_repository=plane_boundary.coordinator.repository,
        plane_runtime=plane_boundary.runtime,
        plane_repositories=plane_boundary.repositories,
    )
    revision = runtimes.create_revision(
        owner_user_id=owner_id,
        agent_id=agent_id,
        artifact_digest=hashlib.sha256(agent_id.encode("utf-8")).hexdigest(),
        manifest={
            "runtime_contract_version": BYO_RUNTIME_CONTRACT_VERSION,
            "files": [],
        },
        artifact_relative_path=f"{agent_id}/revision-1",
        runtime_contract_version=BYO_RUNTIME_CONTRACT_VERSION,
        release_lock_digest=_POLICY.runtime_lock_sha256,
    )
    host = runtimes.register_host_session(
        owner_user_id=owner_id,
        connection_scope_id=str(uuid.uuid4()),
        host_id=str(uuid.uuid4()),
        platform="windows",
        client_version="1.0.0",
        supported_runtime_contract_versions=(BYO_RUNTIME_CONTRACT_VERSION,),
        runtime_lock_sha256=_POLICY.runtime_lock_sha256,
    )
    host = runtimes.mark_inventory_reconciled(host.fence)
    selection = runtimes.select_host_for_agent(
        owner_user_id=owner_id,
        agent_id=agent_id,
    )
    assert selection.session == host
    delivery_operation = _running_operation(
        plane_boundary.coordinator,
        owner_id=owner_id,
        operation_kind="agent_runtime_delivery",
    )
    runtime = runtimes.create_prelaunch_instance(
        owner_user_id=owner_id,
        agent_id=agent_id,
        host_session_id=host.fence.host_session_id,
        revision_id=revision.revision_id,
        operation_fence=delivery_operation,
    )
    runtime = runtimes.bind_runtime_process(
        runtime.fence,
        process_id=str(uuid.uuid4()),
        expected_state_revision=runtime.state_revision,
    )
    if online:
        runtime = runtimes.accept_runtime_registration(
            runtime.fence,
            runtime_contract_version=BYO_RUNTIME_CONTRACT_VERSION,
            bundle_sha256=revision.artifact_digest,
        )
        runtime = runtimes.record_runtime_heartbeat(
            runtime.fence,
            heartbeat_sequence=1,
        )
        runtime = runtimes.mark_runtime_ready(runtime.fence)
        with plane_boundary.runtime.transaction() as transaction:
            transaction.execute(
                "UPDATE user_agent_revision SET state = 'active', "
                "confirmed_at = now(), promoted_at = now(), "
                "state_revision = state_revision + 1 WHERE revision_id = %s",
                (revision.revision_id,),
            )
            transaction.execute(
                "UPDATE agent_runtime_instance SET state = 'online', "
                "is_authoritative = TRUE, state_revision = state_revision + 1 "
                "WHERE runtime_instance_id = %s",
                (runtime.fence.runtime_instance_id,),
            )
            transaction.execute(
                "UPDATE user_agent SET status = 'live', active_revision_id = %s, "
                "last_known_good_revision_id = %s, authoritative_instance_id = %s, "
                "lifecycle_generation = %s, state_revision = state_revision + 1 "
                "WHERE agent_id = %s AND owner_user_id = %s",
                (
                    revision.revision_id,
                    revision.revision_id,
                    runtime.fence.runtime_instance_id,
                    runtime.fence.lifecycle_generation,
                    agent_id,
                    owner_id,
                ),
            )
        runtime = runtimes.get_runtime_instance(runtime.fence.runtime_instance_id)
    return SimpleNamespace(
        owner_id=owner_id,
        agent_id=agent_id,
        runtimes=runtimes,
        revision=revision,
        host=host,
        delivery_operation=delivery_operation,
        runtime=runtime,
    )


def test_revision_activation_and_request_settlement_share_plane_authority(
    plane_boundary: SimpleNamespace,
) -> None:
    owner_id = f"owner-{uuid.uuid4().hex}"
    agent_id = f"agent-{uuid.uuid4().hex}"
    registry = UserAgentRegistry(
        plane_runtime=plane_boundary.runtime,
        plane_repositories=plane_boundary.repositories,
    )
    registry.create(
        agent_id=agent_id,
        owner_user_id=owner_id,
        display_name="Plane runtime agent",
    )
    registry.mark_validated(agent_id, "test-policy-v1")
    runtimes = PersonalAgentRuntimeRepository(
        compatibility_policy=_POLICY,
        operation_repository=plane_boundary.coordinator.repository,
        plane_runtime=plane_boundary.runtime,
        plane_repositories=plane_boundary.repositories,
    )
    host = runtimes.register_host_session(
        owner_user_id=owner_id,
        connection_scope_id=str(uuid.uuid4()),
        host_id=str(uuid.uuid4()),
        platform="windows",
        client_version="1.0.0",
        supported_runtime_contract_versions=(BYO_RUNTIME_CONTRACT_VERSION,),
        runtime_lock_sha256=_POLICY.runtime_lock_sha256,
    )
    host = runtimes.mark_inventory_reconciled(host.fence)
    selection = runtimes.select_host_for_agent(
        owner_user_id=owner_id,
        agent_id=agent_id,
    )
    assert selection.session == host

    revision_id = str(uuid.uuid4())
    bundle_digest = hashlib.sha256(b"agent-bundle").hexdigest()
    runtime_manifest = {
        "manifest_version": 2,
        "digest_algorithm": "sha256",
        "revision_id": revision_id,
        "agent_id": agent_id,
        "agent_name": "Promoted Plane runtime agent",
        "constitution_version": AGENT_CONSTITUTION_VERSION,
        "bundle_sha256": bundle_digest,
        "runtime_contract_version": BYO_RUNTIME_CONTRACT_VERSION,
        "required_runtime_lock_sha256": _POLICY.runtime_lock_sha256,
        "files": [
            {
                "name": name,
                "sha256": hashlib.sha256(name.encode("utf-8")).hexdigest(),
                "size_bytes": len(name),
            }
            for name in BYO_BUNDLE_FILENAMES
        ],
    }
    metadata = CandidateAgentMetadata(
        draft_id=str(uuid.uuid4()),
        draft_state_revision=1,
        display_name="Promoted Plane runtime agent",
        constitution_version=AGENT_CONSTITUTION_VERSION,
        validated_policy_revision=USER_AGENT_POLICY_REVISION,
        declared_tools=("lookup",),
        declared_scopes=("tools:read",),
        declared_egress=("api.example.test",),
    )
    with plane_boundary.runtime.transaction() as transaction:
        agents = plane_boundary.repositories.agents
        drafts = plane_boundary.repositories.draft_agents
        agents.lock_owner(transaction, owner_id=owner_id)
        persisted_agent = agents.get_agent(
            transaction,
            owner_id=owner_id,
            agent_id=agent_id,
            for_update=True,
        )
        assert persisted_agent is not None
        agents.create_revision(
            transaction,
            revision_id=revision_id,
            agent_id=agent_id,
            owner_id=owner_id,
            revision_number=0,
            parent_revision_id=None,
            previous_good_revision_id=None,
            artifact_digest=bundle_digest,
            manifest=runtime_manifest,
            artifact_relative_path=f"{agent_id}/revision-1",
            runtime_contract_version=BYO_RUNTIME_CONTRACT_VERSION,
            release_lock_digest=_POLICY.runtime_lock_sha256,
            compatibility_state="compatible",
            state="prepared",
            promotion_token=str(uuid.uuid4()),
        )
        draft = drafts.create_draft(
            transaction,
            draft_id=metadata.draft_id,
            owner_id=owner_id,
            agent_name=metadata.display_name,
            agent_slug=f"plane-runtime-{metadata.draft_id}",
            description="Plane cutover candidate provenance",
            observed_at=1_720_000_000_000,
            tools_spec=json.dumps(
                [{"name": "lookup", "scope": "tools:read"}],
                sort_keys=True,
            ),
            plan_json=json.dumps(
                {
                    "tools_used": list(metadata.declared_tools),
                    "tool_scopes": {"lookup": "tools:read"},
                    "declared_scopes": list(metadata.declared_scopes),
                    "declared_egress": list(metadata.declared_egress),
                },
                sort_keys=True,
            ),
            constitution_version=metadata.constitution_version,
            draft_uuid=metadata.draft_id,
            target_agent_id=agent_id,
        )
        published_draft = drafts.compare_and_set_draft(
            transaction,
            owner_id=owner_id,
            draft_id=metadata.draft_id,
            expected_revision=draft.state_revision,
            updates={
                "analyze_result": json.dumps(
                    {
                        "passed": True,
                        "constitution_version": metadata.constitution_version,
                        "policy_revision": metadata.validated_policy_revision,
                    },
                    sort_keys=True,
                ),
                "published_revision_id": revision_id,
                "status": "generated",
            },
            updated_at=1_720_000_000_001,
        )
    assert published_draft.state_revision == metadata.draft_state_revision
    assert published_draft.published_revision_id == revision_id
    delivery_operation = _running_operation(
        plane_boundary.coordinator,
        owner_id=owner_id,
        operation_kind="agent_runtime_delivery",
    )
    revision_store = PostgresPersonalAgentRevisionStore(runtimes)
    candidate = revision_store.prepare_candidate(
        CandidatePreparation(
            owner_user_id=owner_id,
            agent_id=agent_id,
            revision_id=revision_id,
            bundle_sha256=bundle_digest,
            runtime_manifest=runtime_manifest,
            artifact_relative_path=f"{agent_id}/revision-1",
            runtime_contract_version=BYO_RUNTIME_CONTRACT_VERSION,
            required_runtime_lock_sha256=_POLICY.runtime_lock_sha256,
            host_session_id=host.fence.host_session_id,
            operation_fence=delivery_operation,
            agent_metadata=metadata,
        )
    )
    revision_store.mark_candidate_starting(candidate)
    delivery = runtimes.get_runtime_instance(candidate.runtime_instance_id)
    starting = runtimes.bind_runtime_process(
        delivery.fence,
        process_id=str(uuid.uuid4()),
        expected_state_revision=delivery.state_revision,
    )
    registered = runtimes.accept_runtime_registration(
        starting.fence,
        runtime_contract_version=BYO_RUNTIME_CONTRACT_VERSION,
        bundle_sha256=bundle_digest,
    )
    live = runtimes.record_runtime_heartbeat(
        registered.fence,
        heartbeat_sequence=1,
    )
    ready = runtimes.mark_runtime_ready(live.fence)
    revision_store.confirm_candidate_ready(candidate, ready.fence.runtime_instance_id)
    commit = revision_store.promote_candidate(candidate)
    assert commit.revision_id == revision_id
    assert revision_store.promote_candidate(candidate) == commit
    with plane_boundary.runtime.transaction() as transaction:
        promoted_agent = plane_boundary.repositories.agents.get_agent(
            transaction,
            owner_id=owner_id,
            agent_id=agent_id,
        )
        persisted_draft = plane_boundary.repositories.draft_agents.get_draft(
            transaction,
            owner_id=owner_id,
            draft_id=metadata.draft_id,
        )
    assert promoted_agent is not None
    assert promoted_agent.display_name == metadata.display_name
    assert promoted_agent.draft_id == metadata.draft_id
    assert tuple(promoted_agent.declared_tools) == metadata.declared_tools
    assert tuple(promoted_agent.declared_scopes) == metadata.declared_scopes
    assert tuple(promoted_agent.declared_egress or ()) == metadata.declared_egress
    assert promoted_agent.constitution_version == metadata.constitution_version
    assert promoted_agent.validated_policy_revision == USER_AGENT_POLICY_REVISION
    assert promoted_agent.validated_at is not None
    assert promoted_agent.revalidation_required is False
    assert promoted_agent.status == "live"
    assert persisted_draft is not None
    assert persisted_draft.status == "generated"
    assert persisted_draft.state_revision == metadata.draft_state_revision
    assert persisted_draft.published_revision_id == revision_id
    online = runtimes.get_current_online_authority(
        owner_user_id=owner_id,
        agent_id=agent_id,
    )
    assert online.state == "online"
    assert online.is_authoritative is True
    assert runtimes.get_current_online_authority(
        owner_user_id=owner_id,
        agent_id=agent_id,
    ).fence == online.fence

    request_operation = _running_operation(
        plane_boundary.coordinator,
        owner_id=owner_id,
        operation_kind="agent_runtime_request",
    )
    request = runtimes.assign_request(
        online.fence,
        operation_fence=request_operation,
    )
    assert runtimes.get_runtime_request(request.fence.request_id) == request
    result_digest = hashlib.sha256(b"runtime-result").hexdigest()
    settled = runtimes.settle_request(
        request.fence,
        state="completed",
        result_digest=result_digest,
    )
    assert settled.state == "completed"
    assert settled.result_digest == result_digest
    assert runtimes.settle_request(
        request.fence,
        state="completed",
        result_digest=result_digest,
    ) == settled

    exit_request_operation = _running_operation(
        plane_boundary.coordinator,
        owner_id=owner_id,
        operation_kind="agent_runtime_request",
    )
    exit_request = runtimes.assign_request(
        online.fence,
        operation_fence=exit_request_operation,
    )
    staged = runtimes.stage_runtime_failure(
        online.fence,
        failure_code="host_lost",
    )
    assert staged.state == "stopping"
    assert staged.is_authoritative is False

    stale_fence = dataclasses.replace(
        staged.fence,
        process_id=str(uuid.uuid4()),
    )
    with pytest.raises(StaleRuntimeGenerationError):
        runtimes.record_runtime_physical_exit(
            stale_fence,
            proof_code="child_exited",
        )
    with pytest.raises(ValueError, match="proof code is invalid"):
        runtimes.record_runtime_physical_exit(
            staged.fence,
            proof_code="host_lost",
        )

    delivery_before_exit = (
        plane_boundary.coordinator.repository.get_operation_for_administration(
            delivery_operation.operation_id
        )
    )
    assert delivery_before_exit is not None
    exact_exit = runtimes.record_runtime_physical_exit(
        staged.fence,
        proof_code="child_exited",
    )
    assert exact_exit.instance.state == "offline"
    assert exact_exit.instance.failure_code == "child_exited"
    assert exact_exit.settled_request_ids == (exit_request.fence.request_id,)

    settled_exit_request = runtimes.get_runtime_request(exit_request.fence.request_id)
    assert settled_exit_request.state == "retryable"
    assert settled_exit_request.terminal_code == "child_exited"
    request_operation_after_exit = (
        plane_boundary.coordinator.repository.get_operation_for_administration(
            exit_request_operation.operation_id
        )
    )
    assert request_operation_after_exit is not None
    assert request_operation_after_exit.state is OperationState.RETRYABLE
    assert request_operation_after_exit.terminal_code == "child_exited"
    delivery_after_exit = (
        plane_boundary.coordinator.repository.get_operation_for_administration(
            delivery_operation.operation_id
        )
    )
    assert delivery_after_exit == delivery_before_exit

    replay = runtimes.record_runtime_physical_exit(
        staged.fence,
        proof_code="child_exited",
    )
    assert replay.instance == exact_exit.instance
    assert replay.settled_request_ids == ()
    upgraded = runtimes.record_runtime_physical_exit(
        staged.fence,
        proof_code="agent_offline",
    )
    assert upgraded.instance.state == "offline"
    assert upgraded.instance.failure_code == "agent_offline"
    assert upgraded.settled_request_ids == ()
    assert runtimes.record_runtime_physical_exit(
        staged.fence,
        proof_code="agent_offline",
    ) == upgraded
    assert (
        plane_boundary.coordinator.repository.get_operation_for_administration(
            delivery_operation.operation_id
        )
        == delivery_before_exit
    )


def test_live_runtime_physical_exit_settles_delivery_and_is_idempotent(
    plane_boundary: SimpleNamespace,
) -> None:
    fixture = _bound_runtime(plane_boundary, online=False)
    runtime = fixture.runtime
    assert runtime.state == "starting"
    stale_fence = dataclasses.replace(
        runtime.fence,
        process_id=str(uuid.uuid4()),
    )
    with pytest.raises(StaleRuntimeGenerationError):
        fixture.runtimes.record_runtime_physical_exit(
            stale_fence,
            proof_code="child_exited",
        )
    with pytest.raises(ValueError, match="proof code is invalid"):
        fixture.runtimes.record_runtime_physical_exit(
            runtime.fence,
            proof_code="host_lost",
        )

    settlement = fixture.runtimes.record_runtime_physical_exit(
        runtime.fence,
        proof_code="child_exited",
    )
    assert settlement.instance.state == "offline"
    assert settlement.instance.failure_code == "child_exited"
    delivery = (
        plane_boundary.coordinator.repository.get_operation_for_administration(
            fixture.delivery_operation.operation_id
        )
    )
    assert delivery is not None
    assert delivery.state is OperationState.RETRYABLE
    assert delivery.terminal_code == "child_exited"

    replay = fixture.runtimes.record_runtime_physical_exit(
        runtime.fence,
        proof_code="child_exited",
    )
    assert replay.instance == settlement.instance
    assert replay.settled_request_ids == ()


def test_staged_permanent_exit_preserves_delivery_operation(
    plane_boundary: SimpleNamespace,
) -> None:
    fixture = _bound_runtime(plane_boundary, online=False)
    fixture.runtimes._operations.terminalize(
        fixture.delivery_operation,
        state=OperationState.FAILED,
        terminal_code="child_registration_timeout",
        safe_summary=None,
        retry_after_ms=None,
        now=None,
        retention=fixture.runtimes._operation_retention,
    )
    staged = fixture.runtimes.stage_runtime_failure(
        fixture.runtime.fence,
        failure_code="child_registration_timeout",
    )
    assert staged.state == "stopping"
    before_exit = (
        plane_boundary.coordinator.repository.get_operation_for_administration(
            fixture.delivery_operation.operation_id
        )
    )
    assert before_exit is not None
    assert before_exit.state is OperationState.FAILED
    assert before_exit.terminal_code == "child_registration_timeout"

    settlement = fixture.runtimes.record_runtime_physical_exit(
        staged.fence,
        proof_code="child_exited",
    )
    assert settlement.instance.state == "offline"
    assert settlement.instance.failure_code == "child_exited"
    assert (
        plane_boundary.coordinator.repository.get_operation_for_administration(
            fixture.delivery_operation.operation_id
        )
        == before_exit
    )
    assert fixture.runtimes.record_runtime_physical_exit(
        staged.fence,
        proof_code="child_exited",
    ).instance == settlement.instance


@pytest.mark.parametrize("cleanup_first", (False, True))
def test_tombstoned_runtime_exit_preserves_agent_deleted_semantics(
    plane_boundary: SimpleNamespace,
    cleanup_first: bool,
) -> None:
    fixture = _bound_runtime(plane_boundary, online=True)
    request_operation = _running_operation(
        plane_boundary.coordinator,
        owner_id=fixture.owner_id,
        operation_kind="agent_runtime_request",
    )
    request = fixture.runtimes.assign_request(
        fixture.runtime.fence,
        operation_fence=request_operation,
    )
    tombstone = fixture.runtimes.tombstone_agent(
        owner_user_id=fixture.owner_id,
        agent_id=fixture.agent_id,
    )
    if cleanup_first:
        cleanup = fixture.runtimes.cleanup_tombstoned_agent(tombstone)
        assert cleanup.settled_request_ids == (request.fence.request_id,)
        cleaned = fixture.runtimes.get_runtime_instance(
            fixture.runtime.fence.runtime_instance_id
        )
        assert cleaned.state == "offline"
        assert cleaned.failure_code == "agent_deleted"

    settlement = fixture.runtimes.record_runtime_physical_exit(
        fixture.runtime.fence,
        proof_code="child_exited",
    )
    assert settlement.instance.state == "offline"
    assert settlement.instance.failure_code == "child_exited"
    expected_settled_ids = () if cleanup_first else (request.fence.request_id,)
    assert settlement.settled_request_ids == expected_settled_ids

    settled_request = fixture.runtimes.get_runtime_request(request.fence.request_id)
    assert settled_request.state == "retryable"
    assert settled_request.terminal_code == "agent_deleted"
    delivery = (
        plane_boundary.coordinator.repository.get_operation_for_administration(
            fixture.delivery_operation.operation_id
        )
    )
    request_delivery = (
        plane_boundary.coordinator.repository.get_operation_for_administration(
            request_operation.operation_id
        )
    )
    assert delivery is not None
    assert delivery.state is OperationState.RETRYABLE
    assert delivery.terminal_code == "agent_deleted"
    assert request_delivery is not None
    assert request_delivery.state is OperationState.RETRYABLE
    assert request_delivery.terminal_code == "agent_deleted"

    replay = fixture.runtimes.record_runtime_physical_exit(
        fixture.runtime.fence,
        proof_code="child_exited",
    )
    assert replay.instance == settlement.instance
    assert replay.settled_request_ids == ()
