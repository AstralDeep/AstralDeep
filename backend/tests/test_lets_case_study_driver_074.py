"""Focused protocol and real-boundary tests for the Feature 074 driver."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests import lets_case_study_driver as driver


_RESULT_KEYS = {
    "format",
    "scenario_id",
    "mode",
    "category",
    "status",
    "execution_identity",
    "observations",
    "measurements",
}
_OBSERVATION_KEYS = {
    "astral_decision",
    "lets_requests",
    "receipts_issued",
    "receipts_claimed",
    "physical_effects",
    "denied_effects",
    "unreceipted_governed_effects",
    "budget_conserved",
    "sequence_monotonic",
    "lifecycle_converged",
    "lifecycle_state",
    "denial_code",
    "scope_binding",
}


def _request(scenario: driver.Scenario) -> bytes:
    return json.dumps(
        scenario.to_request(),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _assert_public_result(
    result: Mapping[str, object], scenario: driver.Scenario
) -> None:
    assert set(result) == _RESULT_KEYS
    assert result["format"] == driver.RESULT_FORMAT
    assert result["scenario_id"] == scenario.scenario_id
    assert result["mode"] == scenario.mode
    assert result["category"] == scenario.category
    assert result["status"] == "passed"
    execution_identity = result["execution_identity"]
    assert isinstance(execution_identity, Mapping)
    assert execution_identity["format"] == driver.EXECUTION_IDENTITY_FORMAT
    assert execution_identity["astraldeep"]["driver_relative_path"] == (  # type: ignore[index]
        driver.DRIVER_RELATIVE_PATH
    )
    observations = result["observations"]
    assert isinstance(observations, Mapping)
    assert set(observations) == _OBSERVATION_KEYS
    assert observations["budget_conserved"] is True
    assert observations["sequence_monotonic"] is True
    assert observations["unreceipted_governed_effects"] == 0
    measurements = result["measurements"]
    assert isinstance(measurements, list) and measurements
    for measurement in measurements:
        assert set(measurement) == {"name", "unit", "samples", "exclusions"}
        assert measurement["samples"]
        assert measurement["exclusions"] == []

    retained = json.dumps(result, ensure_ascii=True, sort_keys=True).casefold()
    for prohibited in (
        "private patient",
        "authorization: bearer",
        "service_token",
        "client_key",
        "c:\\\\users\\",
        "/home/",
        "/work/",
        "/tmp/",
    ):
        assert prohibited not in retained


def test_matrix_is_exactly_nineteen_scenarios_per_mode() -> None:
    for mode in driver.MODES:
        scenarios = driver._scenario_matrix(mode)
        assert len(scenarios) == 19
        assert len({scenario.scenario_id for scenario in scenarios}) == 19
        assert {
            scenario.scope for scenario in scenarios if scenario.category == "scope"
        } == set(driver.TOOL_SCOPES)
        assert {
            scenario.lifecycle_event
            for scenario in scenarios
            if scenario.category == "lifecycle"
        } == set(driver.LIFECYCLE_EVENTS)
        assert {scenario.category for scenario in scenarios}.issuperset(
            {"parallel-dispatch", "recursive-dispatch", *driver.FAULT_SCENARIOS}
        )


def test_imports_resolve_only_from_pinned_component_source_roots() -> None:
    assert Path(driver.astralplane_package.__file__).resolve().parent == (
        driver._ASTRALPLANE_SOURCE_ROOT / "astralplane"
    ).resolve(strict=True)
    assert Path(driver.lets_package.__file__).resolve().parent == (
        driver._LETS_SOURCE_ROOT / "lets"
    ).resolve(strict=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", driver.MODES)
async def test_every_scenario_executes_real_boundaries_and_reports_observations(
    mode: driver._Mode,
) -> None:
    results: dict[str, Mapping[str, object]] = {}
    scenarios = driver._scenario_matrix(mode)
    for scenario in scenarios:
        result = await driver._execute_scenario(scenario)
        _assert_public_result(result, scenario)
        results[scenario.category + ":" + scenario.scenario_id] = result

        observations = result["observations"]
        assert isinstance(observations, Mapping)
        if mode == "off":
            assert observations["lets_requests"] == 0
            assert observations["receipts_issued"] == 0
            assert observations["receipts_claimed"] == 0
        else:
            assert observations["lets_requests"] >= 1
        if mode == "shadow":
            assert observations["receipts_claimed"] == 0

        if scenario.category == "lifecycle":
            assert observations["scope_binding"] is None
            assert observations["astral_decision"] == "not-applicable"
            assert observations["lifecycle_converged"] is True
            assert observations["lifecycle_state"] == scenario.expected_lifecycle_state
        elif scenario.category in driver.FAULT_SCENARIOS and mode == "enforce":
            assert observations["astral_decision"] == "denied"
            assert observations["physical_effects"] == 0
            assert observations["denied_effects"] == 1
            assert (
                observations["denial_code"]
                == driver._FAULT_DENIAL_CODES[scenario.category]
            )
        else:
            assert observations["astral_decision"] == "allowed"
            assert observations["denied_effects"] == 0
        if scenario.category != "lifecycle":
            scope = scenario.scope or "tools:execute"
            expected = driver.binding_for_scope(scope)
            assert observations["scope_binding"] == {
                "scope": expected.scope,
                "capability": expected.capability,
                "transition": expected.transition,
                "resource_dimension": expected.resource_dimension,
                "checks": (
                    0
                    if mode == "off" or scenario.category == "post-revocation-effect"
                    else observations["lets_requests"]
                ),
            }

    parallel = next(
        result
        for key, result in results.items()
        if key.startswith("parallel-dispatch:")
    )
    recursive = next(
        result
        for key, result in results.items()
        if key.startswith("recursive-dispatch:")
    )
    assert parallel["observations"]["physical_effects"] == 4  # type: ignore[index]
    assert recursive["observations"]["physical_effects"] == 4  # type: ignore[index]

    replay = next(
        result for key, result in results.items() if key.startswith("receipt-replay:")
    )
    if mode == "enforce":
        assert replay["observations"]["receipts_issued"] == 1  # type: ignore[index]
        assert replay["observations"]["receipts_claimed"] == 1  # type: ignore[index]
        assert replay["observations"]["denial_code"] == "receipt_replayed"  # type: ignore[index]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"not-json", id="malformed"),
        pytest.param(b"[]", id="array"),
        pytest.param(
            b'{"scenario_id":"off-scope-read","scenario_id":"off-scope-read"}',
            id="duplicate-key",
        ),
        pytest.param(b'{"scenario_id":NaN}', id="non-finite"),
        pytest.param(b"x" * (driver._MAX_STDIN_BYTES + 1), id="oversize"),
    ],
)
def test_strict_parser_rejects_invalid_or_unbounded_input(payload: bytes) -> None:
    with pytest.raises(driver.DriverError):
        driver._load_scenario(payload)


def test_strict_parser_rejects_extra_fields_and_bool_integer_substitution() -> None:
    scenario = driver._scenario_matrix("off")[0]
    extra = scenario.to_request() | {"unexpected": "value"}
    with pytest.raises(driver.DriverError, match="invalid_scenario_shape"):
        driver._load_scenario(json.dumps(extra).encode("ascii"))

    substituted = scenario.to_request() | {"parallelism": True}
    with pytest.raises(driver.DriverError, match="scenario_contract_mismatch"):
        driver._load_scenario(json.dumps(substituted).encode("ascii"))


def test_direct_entrypoint_emits_one_canonical_result_and_no_diagnostic() -> None:
    scenario = next(
        item
        for item in driver._scenario_matrix("enforce")
        if item.category == "receipt-replay"
    )
    completed = subprocess.run(
        [sys.executable, str(Path(driver.__file__).resolve())],
        input=_request(scenario),
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stderr == b""
    assert completed.stdout.count(b"\n") == 1
    result = json.loads(completed.stdout)
    _assert_public_result(result, scenario)
    expected = (
        json.dumps(
            result,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
        + b"\n"
    )
    assert completed.stdout == expected


def test_direct_entrypoint_fails_closed_without_echoing_input_or_paths() -> None:
    completed = subprocess.run(
        [sys.executable, str(Path(driver.__file__).resolve())],
        input=b'{"credential":"do-not-echo"}',
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 2
    assert completed.stdout == b""
    assert completed.stderr == b"case-study driver refused: invalid_scenario_shape\n"
    assert b"do-not-echo" not in completed.stderr


@pytest.mark.asyncio
async def test_unexpected_missing_effect_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = driver._scenario_matrix("off")[0]

    def suppress_effect(
        self: driver._EffectCounter,
        *,
        governed: bool,
        receipted: bool,
        binding: object,
    ) -> str:
        del self, governed, receipted, binding
        return "suppressed"

    monkeypatch.setattr(driver._EffectCounter, "invoke", suppress_effect)
    with pytest.raises(driver.DriverError, match="effect_invariant_failed"):
        await driver._execute_scenario(scenario)


@pytest.mark.asyncio
async def test_scope_mapping_drift_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orchestrator.protected_dispatch as protected_dispatch

    scenario = next(
        item
        for item in driver._scenario_matrix("enforce")
        if item.scenario_id == "enforce-scope-read"
    )
    canonical = driver.binding_for_scope("tools:read")
    execute = driver.binding_for_scope("tools:execute")
    wrong = driver.replace(
        canonical,
        capability=execute.capability,
        transition=execute.transition,
        resource_dimension=execute.resource_dimension,
    )
    monkeypatch.setattr(protected_dispatch, "binding_for_scope", lambda _scope: wrong)
    with pytest.raises(driver.DriverError, match="scope_mapping_invariant_failed"):
        await driver._execute_scenario(scenario)


@pytest.mark.asyncio
async def test_lifecycle_renew_requires_exact_client_call_and_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = next(
        item
        for item in driver._scenario_matrix("enforce")
        if item.scenario_id == "enforce-lifecycle-renew"
    )

    def renew_without_recording(
        self: driver.LifecycleClient,
        *,
        operation_id: str,
        lease_id: str,
        agent_id: str,
        expected_sequence: int,
    ) -> object:
        del operation_id, agent_id
        snapshot = self.snapshots[lease_id]
        assert snapshot.sequence == expected_sequence
        replacement = driver.replace(
            snapshot,
            sequence=snapshot.sequence + 1,
            updated_at_ns=snapshot.updated_at_ns + 1,
        )
        self.snapshots[lease_id] = replacement
        return replacement

    monkeypatch.setattr(driver.LifecycleClient, "renew", renew_without_recording)
    with pytest.raises(driver.DriverError, match="lifecycle_invariant_failed"):
        await driver._execute_scenario(scenario)


@pytest.mark.asyncio
async def test_lifecycle_resume_requires_its_own_sequence_advance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = next(
        item
        for item in driver._scenario_matrix("enforce")
        if item.scenario_id == "enforce-lifecycle-resume"
    )

    def resume_without_advancing(
        self: driver.LifecycleClient,
        *,
        operation_id: str,
        lease_id: str,
        agent_id: str,
    ) -> object:
        assert self.grants[lease_id].subject_id == agent_id
        self._begin("resume", operation_id)
        snapshot = self.snapshots[lease_id]
        replacement = driver.replace(
            snapshot,
            status=type(snapshot.status).ACTIVE,
            updated_at_ns=snapshot.updated_at_ns + 1,
        )
        self.snapshots[lease_id] = replacement
        return replacement

    monkeypatch.setattr(driver.LifecycleClient, "resume", resume_without_advancing)
    with pytest.raises(driver.DriverError, match="scenario_invariant_failed"):
        await driver._execute_scenario(scenario)


@pytest.mark.asyncio
async def test_post_revocation_requires_causal_lookup_exact_state_and_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = next(
        item
        for item in driver._scenario_matrix("enforce")
        if item.category == "post-revocation-effect"
    )
    result = await driver._execute_scenario(scenario)
    for field, value, error in (
        ("lifecycle_converged", False, "revocation_lifecycle_invariant_failed"),
        ("lifecycle_state", "active", "revocation_lifecycle_invariant_failed"),
        ("denial_code", "unrelated_denial", "denial_invariant_failed"),
    ):
        mutated = json.loads(json.dumps(result))
        mutated["observations"][field] = value
        with pytest.raises(driver.DriverError, match=error):
            driver._assert_scenario_result(mutated, scenario)

    def bypass_state(
        self: driver._RevokedBindingRepository,
        _transaction: object,
        **_identity: object,
    ) -> None:
        del self
        return None

    monkeypatch.setattr(
        driver._RevokedBindingRepository,
        "get_active_binding",
        bypass_state,
    )
    with pytest.raises(
        driver.DriverError, match="revocation_causality_invariant_failed"
    ):
        await driver._execute_scenario(scenario)
