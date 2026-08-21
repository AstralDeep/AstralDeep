from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

from astralplane.repositories import RepositoryConflictError as PlaneRepositoryConflictError
from astralplane.repositories import work_admission as plane_admission
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    ExecutionFence,
    OperationState,
    PlaneWorkAdmissionRepository,
    StaleExecutionFenceError,
    WorkAdmissionConflictError,
    WorkAdmissionCoordinator,
)


class _Transaction:
    def __init__(self) -> None:
        self.domain_writes: list[tuple[str, tuple[object, ...]]] = []

    def execute(
        self, statement: str, parameters: tuple[object, ...] = ()
    ) -> object:
        self.domain_writes.append((statement, parameters))
        return SimpleNamespace(rowcount=1, status_message="OK", returned_records=())


class _Runtime:
    def __init__(self, repository: object) -> None:
        self.repositories = SimpleNamespace(work_admission=repository)
        self.opened: list[_Transaction] = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_commit = False

    @contextmanager
    def transaction(self) -> Iterator[_Transaction]:
        transaction = _Transaction()
        self.opened.append(transaction)
        try:
            yield transaction
        except BaseException:
            self.rollbacks += 1
            raise
        else:
            if self.fail_commit:
                self.rollbacks += 1
                raise RuntimeError("commit failed")
            self.commits += 1


def _configs() -> tuple[plane_admission.AdmissionClassConfig, ...]:
    parents = {
        plane_admission.AdmissionClass.GLOBAL: None,
        plane_admission.AdmissionClass.INTERACTIVE: (
            plane_admission.AdmissionClass.GLOBAL
        ),
        plane_admission.AdmissionClass.VOICE_INTERACTIVE: (
            plane_admission.AdmissionClass.INTERACTIVE
        ),
        plane_admission.AdmissionClass.MCP: plane_admission.AdmissionClass.GLOBAL,
        plane_admission.AdmissionClass.BACKGROUND: (
            plane_admission.AdmissionClass.GLOBAL
        ),
        plane_admission.AdmissionClass.SCHEDULED: (
            plane_admission.AdmissionClass.GLOBAL
        ),
        plane_admission.AdmissionClass.MAINTENANCE: (
            plane_admission.AdmissionClass.GLOBAL
        ),
        plane_admission.AdmissionClass.SYSTEM: plane_admission.AdmissionClass.GLOBAL,
    }
    return tuple(
        plane_admission.AdmissionClassConfig(
            class_name=member,
            parent_class_name=parents[member],
            active_limit=2,
            queue_limit=(
                0
                if member is plane_admission.AdmissionClass.VOICE_INTERACTIVE
                else 2
            ),
            max_wait_ms=(
                None
                if member is plane_admission.AdmissionClass.VOICE_INTERACTIVE
                else 5_000
            ),
            config_revision="work-admission-074",
        )
        for member in plane_admission.AdmissionClass
    )


def _record(
    fence: plane_admission.ExecutionFence,
    *,
    state: plane_admission.OperationState = plane_admission.OperationState.RUNNING,
    request_generation: uuid.UUID | None = None,
) -> plane_admission.OperationRecord:
    now = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    return plane_admission.OperationRecord(
        operation_id=fence.operation_id,
        operation_kind="connection_frame",
        admission_class=plane_admission.AdmissionClass.INTERACTIVE,
        owner_scope=plane_admission.OwnerScope.USER,
        owner_user_id="owner-a",
        connection_scope_id=None,
        idempotency_namespace="test",
        idempotency_key="same",
        normalized_input_digest="0" * 64,
        chat_id="chat-a",
        parent_operation_id=None,
        connection_generation=uuid.uuid4(),
        request_generation=request_generation,
        state=state,
        phase_code=None,
        terminal_code=None if state is plane_admission.OperationState.RUNNING else "done",
        safe_summary=None,
        retry_after_ms=None,
        execution_generation=fence.execution_generation,
        execution_lease_token=(
            fence.execution_lease_token
            if state is plane_admission.OperationState.RUNNING
            else None
        ),
        state_revision=1 if state is plane_admission.OperationState.RUNNING else 2,
        accepted_at=now,
        updated_at=now,
        queue_deadline_at=None,
        started_at=now,
        terminal_at=(
            None if state is plane_admission.OperationState.RUNNING else now
        ),
        cancel_requested_at=None,
        purge_after=None,
    )


class _Repository:
    def __init__(self) -> None:
        self.loaded_with: list[object] = []
        self.bound_configs: tuple[plane_admission.AdmissionClassConfig, ...] = ()
        self.configured_with: tuple[plane_admission.AdmissionClassConfig, ...] = ()
        self.asserted_with: list[object] = []
        self.terminalized_with: list[object] = []
        self.oldest_due_with: list[object] = []
        self.admin_reads: list[tuple[object, uuid.UUID, bool]] = []
        self.request_binds: list[
            tuple[object, plane_admission.ExecutionFence, uuid.UUID]
        ] = []
        self.stale = False
        self.conflict = False

    def load_existing_configs(
        self, transaction: object
    ) -> tuple[plane_admission.AdmissionClassConfig, ...]:
        self.loaded_with.append(transaction)
        return _configs()

    def bind_configs(
        self, configs: tuple[plane_admission.AdmissionClassConfig, ...]
    ) -> None:
        self.bound_configs = tuple(configs)

    def configure(
        self,
        transaction: object,
        configs: tuple[plane_admission.AdmissionClassConfig, ...],
    ) -> None:
        del transaction
        self.configured_with = tuple(configs)

    def assert_current_execution(
        self,
        transaction: object,
        fence: plane_admission.ExecutionFence,
    ) -> plane_admission.OperationRecord:
        self.asserted_with.append(transaction)
        if self.stale:
            raise plane_admission.StaleWorkExecutionFenceError("execution fence is stale")
        return _record(fence)

    def terminalize(
        self,
        transaction: object,
        fence: plane_admission.ExecutionFence,
        **_: object,
    ) -> plane_admission.OperationRecord:
        self.terminalized_with.append(transaction)
        return _record(fence, state=plane_admission.OperationState.COMPLETED)

    def oldest_purge_eligible_due_at(
        self, transaction: object, *, now: datetime | None
    ) -> datetime:
        self.oldest_due_with.append(transaction)
        return now or datetime(2026, 8, 14, 12, 0, tzinfo=UTC)

    def get_operation_for_administration(
        self,
        transaction: object,
        *,
        operation_id: uuid.UUID,
        for_update: bool = False,
    ) -> plane_admission.OperationRecord:
        self.admin_reads.append((transaction, operation_id, for_update))
        return _record(
            plane_admission.ExecutionFence(operation_id, 1, uuid.uuid4())
        )

    def bind_request_generation(
        self,
        transaction: object,
        *,
        fence: plane_admission.ExecutionFence,
        request_generation: uuid.UUID,
    ) -> plane_admission.OperationRecord:
        self.request_binds.append((transaction, fence, request_generation))
        if self.conflict:
            raise PlaneRepositoryConflictError(
                "operation is bound to a different request generation"
            )
        return _record(fence, request_generation=request_generation)


def _coordinator() -> tuple[WorkAdmissionCoordinator, _Runtime, _Repository]:
    repository = _Repository()
    runtime = _Runtime(repository)
    coordinator = WorkAdmissionCoordinator.from_plane(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )
    runtime.opened.clear()
    runtime.commits = 0
    return coordinator, runtime, repository


def test_from_plane_binds_the_exact_runtime_catalog_repository() -> None:
    coordinator, runtime, repository = _coordinator()

    assert isinstance(coordinator.repository, PlaneWorkAdmissionRepository)
    assert set(coordinator.repository._configs) == set(AdmissionClass)
    assert len(repository.loaded_with) == 1
    assert repository.bound_configs == _configs()
    assert repository.loaded_with[0] is not None
    assert runtime.rollbacks == 0


def test_configuration_is_not_bound_when_caller_owned_commit_fails() -> None:
    repository = _Repository()
    runtime = _Runtime(repository)
    adapter = PlaneWorkAdmissionRepository(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )
    configs = tuple(
        AdmissionClassConfig(
            class_name=AdmissionClass(config.class_name.value),
            parent_class_name=(
                None
                if config.parent_class_name is None
                else AdmissionClass(config.parent_class_name.value)
            ),
            active_limit=config.active_limit,
            queue_limit=config.queue_limit,
            max_wait_ms=config.max_wait_ms,
            config_revision=config.config_revision,
        )
        for config in _configs()
    )
    runtime.fail_commit = True

    with pytest.raises(RuntimeError, match="commit failed"):
        adapter.configure(configs)

    assert repository.configured_with == _configs()
    assert repository.bound_configs == ()
    assert adapter._configs == {}


def test_fenced_domain_write_and_terminalization_share_one_rollback_scope() -> None:
    coordinator, runtime, repository = _coordinator()
    fence = ExecutionFence(uuid.uuid4(), 1, uuid.uuid4())

    with pytest.raises(RuntimeError, match="domain write failed"):
        with coordinator.fenced_transaction(fence) as transaction:
            transaction.execute("domain repository write", ("value",))
            assert (
                coordinator.oldest_purge_eligible_due_at(transaction=transaction)
                is not None
            )
            terminal = coordinator.terminalize(
                fence,
                state=OperationState.COMPLETED,
                terminal_code=None,
                safe_summary=None,
                retry_after_ms=None,
                transaction=transaction,
            )
            assert terminal.state is OperationState.COMPLETED
            raise RuntimeError("domain write failed")

    assert len(runtime.opened) == 1
    active = runtime.opened[0]
    assert repository.asserted_with == [active]
    assert repository.oldest_due_with == [active]
    assert repository.terminalized_with == [active]
    assert active.domain_writes == [("domain repository write", ("value",))]
    assert runtime.commits == 0
    assert runtime.rollbacks == 1


def test_fenced_transaction_maps_plane_stale_error_without_opening_another_scope() -> None:
    coordinator, runtime, repository = _coordinator()
    repository.stale = True
    fence = ExecutionFence(uuid.uuid4(), 1, uuid.uuid4())

    with pytest.raises(StaleExecutionFenceError, match="stale"):
        with coordinator.fenced_transaction(fence):
            raise AssertionError("stale fence must not yield")

    assert len(runtime.opened) == 1
    assert runtime.rollbacks == 1


def test_administrative_read_and_request_bind_reuse_the_fenced_transaction() -> None:
    coordinator, runtime, repository = _coordinator()
    fence = ExecutionFence(uuid.uuid4(), 1, uuid.uuid4())
    request_generation = uuid.uuid4()

    with coordinator.fenced_transaction(fence) as transaction:
        administrative = coordinator.repository.get_operation_for_administration(
            fence.operation_id,
            for_update=True,
            transaction=transaction,
        )
        bound = coordinator.repository.bind_request_generation(
            fence,
            request_generation,
            transaction=transaction,
        )

    assert administrative is not None
    assert administrative.operation_id == fence.operation_id
    assert administrative.owner_user_id == "owner-a"
    assert bound.request_generation == request_generation
    assert len(runtime.opened) == 1
    active = runtime.opened[0]
    assert repository.asserted_with == [active]
    assert repository.admin_reads == [(active, fence.operation_id, True)]
    assert repository.request_binds[0][0] is active
    assert runtime.commits == 1
    assert runtime.rollbacks == 0


def test_request_generation_conflict_has_a_distinct_deep_error() -> None:
    coordinator, runtime, repository = _coordinator()
    repository.conflict = True
    fence = ExecutionFence(uuid.uuid4(), 1, uuid.uuid4())

    with pytest.raises(WorkAdmissionConflictError, match="different request"):
        coordinator.repository.bind_request_generation(fence, uuid.uuid4())

    assert len(runtime.opened) == 1
    assert runtime.commits == 0
    assert runtime.rollbacks == 1


def test_plane_adapter_refuses_missing_runtime_or_catalog_member() -> None:
    with pytest.raises(TypeError, match="initialized Plane runtime"):
        PlaneWorkAdmissionRepository(plane_runtime=None)

    runtime = _Runtime(_Repository())
    with pytest.raises(TypeError, match="missing work_admission"):
        PlaneWorkAdmissionRepository(
            plane_runtime=runtime,
            plane_repositories=SimpleNamespace(),
        )


def test_deep_work_admission_contains_no_driver_or_sql_implementation() -> None:
    source = Path(__file__).parents[1] / "orchestrator" / "work_admission.py"
    text = source.read_text(encoding="utf-8")

    assert "PostgresWorkAdmissionRepository" not in text
    assert "_get_connection" not in text
    assert "cursor.execute" not in text
    assert "shared.database" not in text
    assert "psycopg" not in text
    for statement in ("SELECT ", "INSERT INTO ", "UPDATE ", "DELETE FROM "):
        assert statement not in text
