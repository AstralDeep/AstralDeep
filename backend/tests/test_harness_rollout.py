"""Qualification, owner selection, binding stability, and rollback tests for the
harness new-work rollout: profiles qualify only on passing conformance evidence for
their exact identity, selections fail closed behind the rollout flag, and in-flight
bindings keep serving their bound profile across rollout transitions.
"""

from __future__ import annotations

import dataclasses
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator.harness import (  # noqa: E402
    HarnessContractError,
    HarnessDeployment,
    HarnessIdentity,
    HarnessSelectionUnavailable,
    HarnessTurnRequest,
    default_catalog,
    run_conformance,
)
from orchestrator.harness.conformance import (  # noqa: E402
    AGENT_ID,
    CREDENTIAL,
    ConformanceReport,
    ScenarioResult,
    TOOL_NAME,
    USER,
)
from orchestrator.harness.contract import (  # noqa: E402
    HarnessAgentOption,
    HarnessToolOption,
)
from orchestrator.harness.rollout import (  # noqa: E402
    HarnessBinding,
    HarnessProfileUnavailable,
    HarnessRollout,
    default_rollout,
)
from orchestrator.harness.selection import ROLLOUT_ENV_VAR, ROLLOUT_FLAG  # noqa: E402
from orchestrator.harness.standard_llm_adapter import (  # noqa: E402
    StandardLLMHarness,
)
from orchestrator.typesafe_routing.budget import UserCircuit  # noqa: E402
from tests.fakes.typesafe_fake import (  # noqa: E402
    FakeTypeSafeClient,
    high_confidence,
)

STANDARD_ID = "standard_openai_compatible"

TYPESAFE_ID = "typesafe_jev_latest"


class Flags:
    def __init__(self, **enabled: bool) -> None:
        self._enabled = enabled

    def is_enabled(self, flag: str) -> bool:
        return self._enabled.get(flag, False)


class BrokenFlags:
    def is_enabled(self, flag: str) -> bool:
        raise RuntimeError("flag registry unavailable")


def _report(deployment: HarnessDeployment, *, passed: bool = True) -> ConformanceReport:
    return ConformanceReport(
        identity=deployment.identity,
        results=(
            ScenarioResult(
                "lifecycle_success", passed, "passed" if passed else "boom"
            ),
        ),
    )


def _tooled_options() -> tuple:
    agents = (HarnessAgentOption(AGENT_ID, "Conformance Agent", "answers turns"),)
    tools = (HarnessToolOption(TOOL_NAME, AGENT_ID, "looks up conditions"),)
    return agents, tools


def _deployment_with_identity(identity: HarnessIdentity) -> HarnessDeployment:
    harness = StandardLLMHarness()
    return HarnessDeployment(
        identity=identity,
        lifecycle=harness,
        tools=harness,
        provider=harness,
        accounting=harness,
    )


class TestQualification:
    def test_qualified_ids_start_empty(self) -> None:
        assert default_rollout().qualified_ids == ()

    def test_qualification_requires_passing_report(self) -> None:
        rollout = default_rollout()
        typesafe = default_catalog().deployment(TYPESAFE_ID)
        with pytest.raises(HarnessContractError, match="failing conformance report"):
            rollout.qualify(typesafe, _report(typesafe, passed=False))
        assert rollout.qualified_ids == ()

    def test_qualification_requires_scenario_results(self) -> None:
        rollout = default_rollout()
        typesafe = default_catalog().deployment(TYPESAFE_ID)
        vacuous = ConformanceReport(identity=typesafe.identity, results=())
        with pytest.raises(HarnessContractError, match="at least one scenario"):
            rollout.qualify(typesafe, vacuous)
        assert rollout.qualified_ids == ()

    def test_qualification_requires_matching_identity(self) -> None:
        rollout = default_rollout()
        typesafe = default_catalog().deployment(TYPESAFE_ID)
        standard = default_catalog().deployment(STANDARD_ID)
        with pytest.raises(HarnessContractError, match="does not match"):
            rollout.qualify(typesafe, _report(standard))
        assert rollout.qualified_ids == ()

    def test_qualification_requires_catalog_membership(self) -> None:
        rollout = default_rollout()
        outsider = _deployment_with_identity(
            HarnessIdentity("outside_runner", "outside", "1.0.0")
        )
        with pytest.raises(HarnessContractError, match="not a member"):
            rollout.qualify(outsider, _report(outsider))
        assert rollout.qualified_ids == ()

    def test_qualification_records_the_exact_identity(self) -> None:
        rollout = default_rollout()
        typesafe = default_catalog().deployment(TYPESAFE_ID)
        qualified = rollout.qualify(typesafe, _report(typesafe))
        assert qualified == typesafe.identity
        assert rollout.qualified_ids == (TYPESAFE_ID,)

    def test_qualification_rejects_wrong_types(self) -> None:
        rollout = default_rollout()
        typesafe = default_catalog().deployment(TYPESAFE_ID)
        with pytest.raises(HarnessContractError, match="harness deployment"):
            rollout.qualify("typesafe_jev_latest", _report(typesafe))
        with pytest.raises(HarnessContractError, match="conformance report"):
            rollout.qualify(typesafe, "report")

    def test_catalog_accessor_exposes_the_bound_catalog(self) -> None:
        catalog = default_catalog()
        assert HarnessRollout(catalog).catalog is catalog


class TestSelection:
    def test_select_requires_rollout_flag(self) -> None:
        rollout = default_rollout()
        with pytest.raises(
            HarnessProfileUnavailable, match="rollout_flag_disabled"
        ) as excinfo:
            rollout.select(STANDARD_ID, owner_id=USER)
        assert excinfo.value.reason == "rollout_flag_disabled"
        assert isinstance(excinfo.value, HarnessSelectionUnavailable)

    def test_select_fails_closed_on_broken_flag_source(self) -> None:
        rollout = HarnessRollout(flags=BrokenFlags())
        with pytest.raises(HarnessProfileUnavailable, match="rollout_flag_disabled"):
            rollout.select(STANDARD_ID, owner_id=USER)

    def test_select_unknown_profile(self) -> None:
        rollout = HarnessRollout(flags=Flags(**{ROLLOUT_FLAG: True}))
        with pytest.raises(HarnessProfileUnavailable, match="unknown_profile") as e:
            rollout.select("missing_runner", owner_id=USER)
        assert e.value.reason == "unknown_profile"

    def test_select_requires_qualification_for_non_default(self) -> None:
        rollout = HarnessRollout(flags=Flags(**{ROLLOUT_FLAG: True}))
        with pytest.raises(HarnessProfileUnavailable, match="not_qualified") as e:
            rollout.select(TYPESAFE_ID, owner_id=USER)
        assert e.value.reason == "not_qualified"

    def test_select_default_needs_only_the_flag(self) -> None:
        catalog = default_catalog()
        rollout = HarnessRollout(catalog, flags=Flags(**{ROLLOUT_FLAG: True}))
        binding = rollout.select(STANDARD_ID, owner_id=USER)
        assert isinstance(binding, HarnessBinding)
        assert binding.harness_id == STANDARD_ID
        assert binding.identity == catalog.deployment(STANDARD_ID).identity
        assert binding.owner_id == USER
        assert binding.deployment is catalog.deployment(STANDARD_ID)

    def test_select_qualified_profile_returns_binding(self) -> None:
        catalog = default_catalog()
        rollout = HarnessRollout(catalog, flags=Flags(**{ROLLOUT_FLAG: True}))
        typesafe = catalog.deployment(TYPESAFE_ID)
        rollout.qualify(typesafe, _report(typesafe))
        binding = rollout.select(TYPESAFE_ID, owner_id=USER)
        assert binding.harness_id == TYPESAFE_ID
        assert binding.deployment is typesafe

    def test_owner_id_is_required(self) -> None:
        rollout = HarnessRollout(flags=Flags(**{ROLLOUT_FLAG: True}))
        for owner in ("", "x" * 257):
            with pytest.raises(HarnessContractError, match="owner_id"):
                rollout.select(STANDARD_ID, owner_id=owner)

    def test_rollout_flag_defaults_off_in_the_registry(self, monkeypatch) -> None:
        monkeypatch.delenv(ROLLOUT_ENV_VAR, raising=False)
        from shared.feature_flags import flags as global_flags

        monkeypatch.setattr(global_flags, "_flags", {})
        assert global_flags.is_enabled(ROLLOUT_FLAG) is False
        monkeypatch.setenv(ROLLOUT_ENV_VAR, "true")
        from shared.feature_flags import FeatureFlags

        assert FeatureFlags()._flags[ROLLOUT_FLAG] is True


class TestNewWorkRollout:
    def test_new_work_starts_on_catalog_default(self) -> None:
        assert default_rollout().target == STANDARD_ID

    def test_select_new_work_serves_default_before_any_enable(self) -> None:
        rollout = HarnessRollout(flags=Flags(**{ROLLOUT_FLAG: True}))
        binding = rollout.select_new_work(owner_id=USER)
        assert binding.harness_id == STANDARD_ID

    def test_select_new_work_requires_flag(self) -> None:
        with pytest.raises(HarnessProfileUnavailable, match="rollout_flag_disabled"):
            default_rollout().select_new_work(owner_id=USER)

    def test_enable_requires_qualification(self) -> None:
        rollout = HarnessRollout(flags=Flags(**{ROLLOUT_FLAG: True}))
        with pytest.raises(HarnessProfileUnavailable, match="not_qualified"):
            rollout.enable(TYPESAFE_ID)
        assert rollout.target == STANDARD_ID

    def test_enable_unknown_profile(self) -> None:
        rollout = HarnessRollout(flags=Flags(**{ROLLOUT_FLAG: True}))
        with pytest.raises(HarnessProfileUnavailable, match="unknown_profile"):
            rollout.enable("missing_runner")

    def test_enable_rolls_new_work_forward(self) -> None:
        rollout = HarnessRollout(flags=Flags(**{ROLLOUT_FLAG: True}))
        typesafe = default_catalog().deployment(TYPESAFE_ID)
        rollout.qualify(typesafe, _report(typesafe))
        transition = rollout.enable(TYPESAFE_ID)
        assert transition.action == "enable"
        assert transition.changed is True
        assert transition.previous_harness_id == STANDARD_ID
        assert transition.target_harness_id == TYPESAFE_ID
        assert rollout.target == TYPESAFE_ID
        assert rollout.select_new_work(owner_id=USER).harness_id == TYPESAFE_ID

    def test_enable_default_is_the_shipped_baseline(self) -> None:
        rollout = HarnessRollout(flags=Flags(**{ROLLOUT_FLAG: True}))
        transition = rollout.enable(STANDARD_ID)
        assert transition.changed is False
        assert rollout.target == STANDARD_ID

    def test_rollback_reverts_new_work_to_default(self) -> None:
        rollout = HarnessRollout(flags=Flags(**{ROLLOUT_FLAG: True}))
        typesafe = default_catalog().deployment(TYPESAFE_ID)
        rollout.qualify(typesafe, _report(typesafe))
        rollout.enable(TYPESAFE_ID)
        transition = rollout.rollback()
        assert transition.action == "rollback"
        assert transition.changed is True
        assert transition.previous_harness_id == TYPESAFE_ID
        assert transition.target_harness_id == STANDARD_ID
        assert rollout.target == STANDARD_ID
        assert rollout.select_new_work(owner_id=USER).harness_id == STANDARD_ID

    def test_rollback_is_idempotent(self) -> None:
        rollout = HarnessRollout(flags=Flags(**{ROLLOUT_FLAG: True}))
        transition = rollout.rollback()
        assert transition.changed is False
        assert transition.previous_harness_id == STANDARD_ID
        assert rollout.target == STANDARD_ID

    def test_transitions_stage_while_selection_stays_fail_closed(self) -> None:
        rollout = HarnessRollout(flags=Flags(**{ROLLOUT_FLAG: False}))
        typesafe = default_catalog().deployment(TYPESAFE_ID)
        rollout.qualify(typesafe, _report(typesafe))
        assert rollout.enable(TYPESAFE_ID).changed is True
        assert rollout.target == TYPESAFE_ID
        with pytest.raises(HarnessProfileUnavailable, match="rollout_flag_disabled"):
            rollout.select_new_work(owner_id=USER)
        with pytest.raises(HarnessProfileUnavailable, match="rollout_flag_disabled"):
            rollout.select(TYPESAFE_ID, owner_id=USER)
        assert rollout.rollback().changed is True
        assert rollout.target == STANDARD_ID


class TestBindingStability:
    async def test_binding_keeps_serving_its_profile_across_rollback(self) -> None:
        rollout = HarnessRollout(flags=Flags(**{ROLLOUT_FLAG: True}))
        typesafe = default_catalog().deployment(TYPESAFE_ID)
        rollout.qualify(typesafe, _report(typesafe))
        rollout.enable(TYPESAFE_ID)
        binding = rollout.select_new_work(owner_id=USER)
        assert binding.harness_id == TYPESAFE_ID

        rollout.rollback()
        assert rollout.target == STANDARD_ID

        assert binding.harness_id == TYPESAFE_ID
        agents, tools = _tooled_options()
        request = HarnessTurnRequest(
            user_id=USER,
            message="in-flight binding message",
            agents=agents,
            tools=tools,
            api_key=CREDENTIAL,
            fingerprint="binding-fingerprint",
            transport={
                "circuit": UserCircuit(),
                "client": FakeTypeSafeClient(
                    default=high_confidence(AGENT_ID, TOOL_NAME)
                ),
            },
        )
        execution = await binding.deployment.lifecycle.start(request)
        assert execution.identity == typesafe.identity
        result = await binding.deployment.lifecycle.await_result(execution)
        assert result.outcome.value == "succeeded"
        assert result.decision is not None

    def test_binding_is_frozen(self) -> None:
        rollout = HarnessRollout(flags=Flags(**{ROLLOUT_FLAG: True}))
        binding = rollout.select(STANDARD_ID, owner_id=USER)
        with pytest.raises(dataclasses.FrozenInstanceError):
            binding.owner_id = "someone-else"


class TestConformanceBoundRollout:
    async def test_shipped_profiles_qualify_through_conformance_and_roll(self) -> None:
        rollout = HarnessRollout(flags=Flags(**{ROLLOUT_FLAG: True}))
        catalog = default_catalog()
        for harness_id in (STANDARD_ID, TYPESAFE_ID):
            deployment = catalog.deployment(harness_id)
            report = await run_conformance(deployment)
            assert report.passed, report.failures
            rollout.qualify(deployment, report)
        assert rollout.qualified_ids == (STANDARD_ID, TYPESAFE_ID)

        transition = rollout.enable(TYPESAFE_ID)
        assert transition.changed is True
        assert rollout.select_new_work(owner_id=USER).harness_id == TYPESAFE_ID

        assert rollout.rollback().changed is True
        assert rollout.select_new_work(owner_id=USER).harness_id == STANDARD_ID

    async def test_stale_conformance_evidence_cannot_qualify(self) -> None:
        rollout = HarnessRollout(flags=Flags(**{ROLLOUT_FLAG: True}))
        standard = default_catalog().deployment(STANDARD_ID)
        stale = _deployment_with_identity(
            HarnessIdentity(
                standard.identity.harness_id,
                standard.identity.profile,
                "0.0.9",
            )
        )
        stale_report = await run_conformance(stale)
        assert stale_report.passed
        with pytest.raises(HarnessContractError, match="does not match"):
            rollout.qualify(standard, stale_report)
        assert rollout.qualified_ids == ()
