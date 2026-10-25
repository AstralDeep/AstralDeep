"""Executes the mesh availability topology conformance cases against the current
snapshot, binding the decided single-durable-authority posture to live repository
mechanics. The suite is deterministic (no network, no live database, no clock bounds)
and fails closed when the repository drifts from the decided posture.
"""

from __future__ import annotations

import inspect
import json
import re
import uuid
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from astralplane.errors import SQLContractError
from astralplane.outbox import PostgresOutboxStore
from astralplane.repositories import work_admission as plane_admission
from orchestrator.mesh_presence import ttl_seconds
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    ExecutionFence,
    StaleExecutionFenceError,
    WorkAdmissionCoordinator,
)
from orchestrator.work_continuations import WorkReconcileRequest

REPO = Path(__file__).resolve().parents[2]
CASES_PATH = (
    REPO / "specs" / "100-mesh-availability-topology" / "contracts" / "availability-cases.json"
)
CONTRACT_PATH = (
    REPO
    / "specs"
    / "100-mesh-availability-topology"
    / "contracts"
    / "availability-contract.md"
)

KNOWN_KINDS = {
    "case_registry",
    "durable_authority_boundary",
    "lease_fencing_surface",
    "stale_fence_invalidated",
    "uncertain_effect_owner_decision",
    "executor_loss_honest_outcome",
    "outbox_fenced_redelivery",
    "postgres_loss_fails_closed",
    "restore_attestation_gate",
    "blob_root_fail_closed",
    "presence_honest_staleness",
    "recovery_promise_evidence",
    "failure_matrix_completeness",
}

CASE_BINDINGS = {
    "C-01": "test_c01_case_registry_is_wellformed_and_bound",
    "C-02": "test_c02_single_durable_authority_boundary",
    "C-03": "test_c03_lease_and_fence_mechanics_exist",
    "C-04": "test_c04_stale_fence_cannot_terminalize",
    "C-05": "test_c05_uncertain_effect_needs_owner_decision",
    "C-06": "test_c06_executor_loss_is_honest",
    "C-07": "test_c07_outbox_redelivery_is_fenced",
    "C-08": "test_c08_composition_drift_fails_closed",
    "C-09": "test_c09_restore_requires_attestation",
    "C-10": "test_c10_blob_root_resolution_fails_closed",
    "C-11": "test_c11_presence_is_ttl_bounded_and_honest",
    "C-12": "test_c12_promises_carry_required_evidence",
    "C-13": "test_c13_failure_matrix_is_complete",
}


def _load() -> dict:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def _contract() -> str:
    return CONTRACT_PATH.read_text(encoding="utf-8")


def _contract_plain() -> str:
    return _contract().replace("*", "").replace("`", "")


def _case(case_id: str) -> dict:
    for case in _load()["cases"]:
        if case["id"] == case_id:
            return case
    raise AssertionError(f"missing conformance case {case_id}")


class _FenceRepository:
    def __init__(self) -> None:
        self.stale = False
        self.configured: tuple[AdmissionClassConfig, ...] | None = None

    def configure(self, configs) -> None:
        self.configured = tuple(configs)

    def assert_current_execution(self, fence, transaction=None):
        del transaction
        if self.stale:
            raise StaleExecutionFenceError("execution fence is stale")
        return fence

    def assert_current_execution_lease(self, fence, transaction=None):
        del transaction
        if self.stale:
            raise StaleExecutionFenceError("execution lease is stale")
        return fence

    def reselect_execution(self, fence, now=None, slot_lease=None):
        return ExecutionFence(
            fence.operation_id, fence.execution_generation + 1, uuid.uuid4()
        )


def _coordinator(repository: _FenceRepository) -> WorkAdmissionCoordinator:
    config = AdmissionClassConfig(
        class_name=AdmissionClass.BACKGROUND,
        parent_class_name=AdmissionClass.GLOBAL,
        active_limit=1,
        queue_limit=1,
        max_wait_ms=1000,
        config_revision="mesh-availability-100",
    )
    return WorkAdmissionCoordinator(
        admission_classes=[config, AdmissionClassConfig(
            class_name=AdmissionClass.GLOBAL,
            parent_class_name=None,
            active_limit=1,
            queue_limit=1,
            max_wait_ms=1000,
            config_revision="mesh-availability-100",
        )],
        repository=repository,
    )


def test_c01_case_registry_is_wellformed_and_bound():
    case = _case("C-01")
    data = _load()
    assert data["contract"] == (
        "specs/100-mesh-availability-topology/contracts/availability-contract.md"
    )
    assert len(data["cases"]) == case["expect"]["case_count"]
    ids = [entry["id"] for entry in data["cases"]]
    assert len(ids) == len(set(ids))
    for entry in data["cases"]:
        assert re.fullmatch(r"C-\d{2}", entry["id"])
        assert entry["kind"] in KNOWN_KINDS
        assert entry["title"]
        assert isinstance(entry["expect"], dict)
    plain = _contract_plain()
    assert data["status"] in plain
    assert data["topology"].split()[0] in plain
    assert set(ids) == set(CASE_BINDINGS)
    for case_id, test_name in CASE_BINDINGS.items():
        assert f"**{case_id}**" in _contract()
        assert callable(globals().get(test_name)), f"{case_id} unbound"


def test_c02_single_durable_authority_boundary():
    case = _case("C-02")
    expect = case["expect"]
    assert not (REPO / expect["legacy_database_module"]).exists()
    manifest = json.loads((REPO / expect["composition_manifest"]).read_text("utf-8"))
    compatibility = manifest["compatibility"]
    assert isinstance(compatibility.get("data_plane"), dict)
    assert set(compatibility["data_plane"]) == {
        "contract_version",
        "schema_revision",
        "read_compatible_from",
        "migration_sha256",
        "blob_layout_version",
    }
    requirements = (REPO / expect["requirements_manifest"]).read_text("utf-8").lower()
    for driver in expect["forbidden_drivers"]:
        assert driver not in requirements, driver
    assert expect["deferred_marker"] in _contract_plain()


def test_c03_lease_and_fence_mechanics_exist():
    case = _case("C-03")
    expect = case["expect"]
    default = inspect.signature(WorkAdmissionCoordinator.__init__).parameters[
        "slot_lease"
    ].default
    assert default == timedelta(seconds=expect["slot_lease_seconds"])
    for name in expect["methods"]:
        assert callable(getattr(WorkAdmissionCoordinator, name)), name
    assert hasattr(plane_admission, expect["plane_error"])


def test_c04_stale_fence_cannot_terminalize():
    case = _case("C-04")
    expect = case["expect"]
    assert inspect.signature(WorkAdmissionCoordinator.__init__).parameters[
        "slot_lease"
    ].default == timedelta(seconds=expect["coordinator_slot_lease_seconds"])
    repository = _FenceRepository()
    coordinator = _coordinator(repository)
    assert repository.configured is not None
    fence = ExecutionFence(uuid.uuid4(), expect["stale_generation"], uuid.uuid4())
    assert coordinator.assert_current_execution(fence) is fence
    repository.stale = True
    with pytest.raises(StaleExecutionFenceError):
        coordinator.assert_current_execution(fence)
    with pytest.raises(StaleExecutionFenceError):
        coordinator.assert_current_execution_lease(fence, transaction=object())
    repository.stale = False
    reselected = coordinator.reselect_execution(fence)
    assert reselected.execution_generation == expect["reselected_generation"]
    assert reselected.execution_lease_token != fence.execution_lease_token
    assert reselected.operation_id == fence.operation_id


def test_c05_uncertain_effect_needs_owner_decision():
    case = _case("C-05")
    expect = case["expect"]
    valid = dict(expect["valid"])
    valid["prior_result_digest"] = expect["valid"]["prior_result_digest"] * 16
    assert len(valid["prior_result_digest"]) == expect["digest_hex"]
    accepted = WorkReconcileRequest.model_validate(valid)
    assert accepted.decision in expect["decisions"]
    for decision in expect["invalid_decisions"]:
        with pytest.raises(ValidationError):
            WorkReconcileRequest.model_validate({**valid, "decision": decision})
    with pytest.raises(ValidationError):
        WorkReconcileRequest.model_validate(
            {**valid, "prior_result_digest": "ab" * 63}
        )
    decisions_field = WorkReconcileRequest.model_fields["decision"]
    assert set(decisions_field.annotation.__args__) == set(expect["decisions"])


def test_c06_executor_loss_is_honest():
    from orchestrator import remote_jobs

    case = _case("C-06")
    expect = case["expect"]
    outcome = remote_jobs._job_outcome
    assert outcome("COMPLETED", "0", confirmed=True) == expect["confirmed_completed"]
    assert outcome("COMPLETED", "0", confirmed=False) == expect["unconfirmed_completed"]
    assert outcome("ORPHANED", None) == expect["orphaned"]
    assert remote_jobs.WAKE_EVENT_PREFIX == expect["wake_prefix"]
    for handler in expect["handlers"]:
        assert callable(getattr(remote_jobs, handler)), handler


def test_c07_outbox_redelivery_is_fenced():
    case = _case("C-07")
    expect = case["expect"]
    store = PostgresOutboxStore
    for name in expect["methods"]:
        assert callable(getattr(store, name)), name
    claim_params = inspect.signature(store.claim).parameters
    for name in expect["claim_params"]:
        assert name in claim_params, name
    assert "lease_duration" in claim_params
    fence = store._fence_inputs
    bound = fence("entry-1", "worker-1", 3)
    assert bound == ("entry-1", "worker-1", 3)
    assert bound[: len(expect["fence_fields"])] == ("entry-1", "worker-1", 3)
    if expect["bad_version_raises"]:
        with pytest.raises(SQLContractError):
            fence("entry-1", "worker-1", 0)


def test_c08_composition_drift_fails_closed():
    from orchestrator.plane_composition import (
        PlaneCompositionError,
        compose_plane_catalog,
        inspect_plane_composition,
        load_plane_expectation,
    )

    case = _case("C-08")
    expect = case["expect"]
    expectation = load_plane_expectation(REPO / expect["manifest"])
    assert inspect_plane_composition(expectation).compatible
    tampered = replace(expectation, **{expect["tampered_field"]: expect["tampered_value"]})
    report = inspect_plane_composition(tampered)
    assert not report.compatible
    assert expect["expected_reason"] in report.reasons
    with pytest.raises(PlaneCompositionError):
        compose_plane_catalog(tampered)


def test_c09_restore_requires_attestation(monkeypatch):
    import astralplane.recovery as recovery

    case = _case("C-09")
    expect = case["expect"]
    function = getattr(recovery, expect["function"])
    parameters = inspect.signature(function).parameters
    assert set(parameters) == set(expect["keyword_only_params"])
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in parameters.values()
    )

    def must_not_connect(*args, **kwargs):
        pytest.fail("incompatible restore attestation attempted a connection")

    monkeypatch.setattr(recovery, "create_postgres_driver_pool", must_not_connect)
    with pytest.raises(getattr(recovery, expect["error"])):
        function(
            database_url="postgresql://127.0.0.1:1/none",
            expected_database="none",
            expected_schema="public",
            expected_schema_revision=expect["incompatible_revision"],
            expected_migration_digest="0" * 64,
        )


def test_c10_blob_root_resolution_fails_closed():
    from orchestrator.plane_composition import (
        PlaneCompositionError,
        resolve_plane_blob_root,
    )

    case = _case("C-10")
    expect = case["expect"]
    resolver = resolve_plane_blob_root
    assert resolver({}).name == expect["default_dir"]
    for invalid in expect["invalid"]:
        with pytest.raises(PlaneCompositionError):
            resolver({expect["env"]: invalid})


def test_c11_presence_is_ttl_bounded_and_honest(monkeypatch):
    from orchestrator import mesh_presence

    case = _case("C-11")
    expect = case["expect"]
    assert mesh_presence.PRESENCE_OFFLINE == expect["offline"]
    assert mesh_presence.PRESENCE_UNAVAILABLE == expect["unavailable"]
    assert mesh_presence.REASON_STALE_OBSERVATION == expect["stale_reason"]
    assert mesh_presence.REASON_UNPUBLISHED == expect["unpublished_reason"]
    monkeypatch.delenv("MESH_PRESENCE_TTL_SECONDS", raising=False)
    assert ttl_seconds() == expect["default_ttl"]
    monkeypatch.setenv("MESH_PRESENCE_TTL_SECONDS", "10")
    assert ttl_seconds() == expect["min_ttl"]
    monkeypatch.setenv("MESH_PRESENCE_TTL_SECONDS", "99999")
    assert ttl_seconds() == expect["max_ttl"]
    monkeypatch.setenv("MESH_PRESENCE_TTL_SECONDS", expect["non_numeric_env"])
    assert ttl_seconds() == expect["default_ttl"]


def test_c12_promises_carry_required_evidence():
    case = _case("C-12")
    data = _load()
    expect = case["expect"]
    assert len(data["promises"]) == expect["promise_count"]
    plain = _contract_plain()
    for promise in data["promises"]:
        assert re.fullmatch(r"P-\d", promise["id"])
        assert promise["status"] == expect["status"]
        assert promise["promise"]
        assert promise["required_evidence"]
        assert f"**{promise['id']}**" in _contract()
        case_ids = re.findall(r"C-\d{2}", promise["required_evidence"])
        assert case_ids, promise["id"]
        for case_id in case_ids:
            assert case_id in {entry["id"] for entry in data["cases"]}
    assert f"**P-{len(data['promises'])}**" in _contract()
    assert f"P-{len(data['promises'])}" in plain


def test_c13_failure_matrix_is_complete():
    case = _case("C-13")
    data = _load()
    expect = case["expect"]
    matrix = data["matrix"]
    assert [row["id"] for row in matrix] == expect["failure_classes"]
    for row in matrix:
        for field in expect["required_fields"]:
            assert row.get(field), f"{row['id']} missing {field}"
        assert f"**{row['id']}**" in _contract()
    assert len({row["failure"] for row in matrix}) == len(matrix)


@pytest.mark.parametrize("case_id", sorted(CASE_BINDINGS))
def test_contract_case_registry_stays_complete(case_id):
    assert f"**{case_id}**" in _contract()
    _case(case_id)
