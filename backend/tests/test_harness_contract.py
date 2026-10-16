"""Conformance and fail-closed gating tests for the versioned harness contract:
both shipped runner adapters must pass the deterministic conformance pack, the
catalog must refuse incomplete or unqualified deployments, and profile selection
must stay unavailable until a qualified rollout record exists.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import replace
from typing import Any

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from llm_config.invocation import InvocationResult  # noqa: E402

from orchestrator.harness import (  # noqa: E402
    HARNESS_CONTRACT_VERSION,
    AccountingSeam,
    ConformanceReport,
    HarnessAccounting,
    HarnessAgentOption,
    HarnessContractError,
    HarnessDeployment,
    HarnessIdentity,
    HarnessOutcome,
    HarnessProviderFrame,
    HarnessSelectionUnavailable,
    HarnessToolOption,
    HarnessToolSurface,
    HarnessTurnRequest,
    LifecycleSeam,
    ProviderSeam,
    ToolSeam,
    assert_credential_free,
    default_catalog,
    empty_surface,
    run_conformance,
    standard_deployment,
    typesafe_deployment,
    validate_deployment,
)
from orchestrator.harness import conformance  # noqa: E402
from orchestrator.harness.conformance import (  # noqa: E402
    AGENT_ID,
    CREDENTIAL,
    TOOL_NAME,
    USER,
)
from orchestrator.harness.selection import (  # noqa: E402
    ROLLOUT_ENV_VAR,
    ROLLOUT_FLAG,
    HarnessCatalog,
    validate_catalog_deployment,
)
from orchestrator.harness.standard_llm_adapter import (  # noqa: E402
    StandardLLMHarness,
)
from orchestrator.harness.typesafe_adapter import (  # noqa: E402
    TypeSafeRoutingHarness,
)
from orchestrator.typesafe_routing.budget import (  # noqa: E402
    Outcome,
    UserCircuit,
)
from tests.fakes.typesafe_fake import (  # noqa: E402
    FakeTypeSafeClient,
    TypeSafeAuthenticationError,
    TypeSafeAPITimeoutError,
    high_confidence,
)


class Flags:
    def __init__(self, **enabled: bool) -> None:
        self._enabled = enabled

    def is_enabled(self, flag: str) -> bool:
        return self._enabled.get(flag, False)


def _turn_request(**overrides: Any) -> HarnessTurnRequest:
    agents = (HarnessAgentOption(AGENT_ID, "Conformance Agent", "answers turns"),)
    tools = (HarnessToolOption(TOOL_NAME, AGENT_ID, "looks up conditions"),)
    fields: dict[str, Any] = {
        "user_id": USER,
        "message": "what is the conformance answer",
        "agents": agents,
        "tools": tools,
        "api_key": CREDENTIAL,
        "fingerprint": "fingerprint",
    }
    fields.update(overrides)
    return HarnessTurnRequest(**fields)


def _typesafe_harness(**overrides: Any) -> TypeSafeRoutingHarness:
    fields: dict[str, Any] = {"circuit": UserCircuit()}
    fields.update(overrides)
    return TypeSafeRoutingHarness(**fields)


def _typesafe_transport(script: str) -> dict[str, Any]:
    if script == "success":
        return {
            "client": FakeTypeSafeClient(default=high_confidence(AGENT_ID, TOOL_NAME))
        }
    if script == "slow":

        async def _block(seconds: float) -> None:
            del seconds
            await asyncio.Event().wait()

        return {
            "client": FakeTypeSafeClient(
                default=high_confidence(AGENT_ID, TOOL_NAME),
                latency=0.05,
                sleep=_block,
            )
        }
    if script == "auth_failure":
        return {
            "client": FakeTypeSafeClient(
                default=high_confidence(AGENT_ID, TOOL_NAME),
                faults=[TypeSafeAuthenticationError],
            )
        }
    if script == "transient_failure":
        return {
            "client": FakeTypeSafeClient(
                default=high_confidence(AGENT_ID, TOOL_NAME),
                faults=[TypeSafeAPITimeoutError] * 3,
            )
        }
    raise ValueError(f"unknown script {script!r}")


async def _run_typesafe(
    script: str, harness: TypeSafeRoutingHarness | None = None, **request_overrides: Any
):
    harness = harness or _typesafe_harness()
    request = _turn_request(transport=_typesafe_transport(script), **request_overrides)
    execution = await harness.start(request)
    return await harness.await_result(execution), execution, harness


class TestContractIdentity:
    def test_identity_accepts_bounded_values(self) -> None:
        identity = HarnessIdentity("a_runner", "profile", "1.2.3")
        assert identity.contract_version == HARNESS_CONTRACT_VERSION

    @pytest.mark.parametrize(
        "harness_id",
        ["", "Upper", "has-dash", "x" * 65, "1starts_digit"],
    )
    def test_identity_rejects_bad_harness_id(self, harness_id: str) -> None:
        with pytest.raises(HarnessContractError):
            HarnessIdentity(harness_id, "profile", "1.0.0")

    @pytest.mark.parametrize("profile", ["", "Upper", "x" * 65])
    def test_identity_rejects_bad_profile(self, profile: str) -> None:
        with pytest.raises(HarnessContractError):
            HarnessIdentity("runner", profile, "1.0.0")

    @pytest.mark.parametrize("version", ["", "1.0", "v1.0.0", "1.0.0.0"])
    def test_identity_rejects_bad_adapter_version(self, version: str) -> None:
        with pytest.raises(HarnessContractError):
            HarnessIdentity("runner", "profile", version)

    def test_identity_rejects_foreign_contract_version(self) -> None:
        with pytest.raises(HarnessContractError):
            HarnessIdentity("runner", "profile", "1.0.0", contract_version="2")


class TestProviderFrames:
    def test_frame_accepts_valid_posture(self) -> None:
        frame = HarnessProviderFrame(
            provider_key="openai",
            model="gpt-x",
            base_url="https://api.openai.com/v1",
            credential_source="user",
            local=False,
            keyless=False,
        )
        assert frame.provider_key == "openai"

    def test_frame_rejects_bad_provider_key(self) -> None:
        with pytest.raises(HarnessContractError):
            HarnessProviderFrame(
                provider_key="Bad Key",
                model="m",
                base_url="https://x",
                credential_source="user",
                local=False,
                keyless=False,
            )

    def test_frame_rejects_overlong_model(self) -> None:
        with pytest.raises(HarnessContractError):
            HarnessProviderFrame(
                provider_key="openai",
                model="m" * 257,
                base_url="https://x",
                credential_source="user",
                local=False,
                keyless=False,
            )

    @pytest.mark.parametrize("base_url", ["", "x" * 2049])
    def test_frame_rejects_bad_base_url(self, base_url: str) -> None:
        with pytest.raises(HarnessContractError):
            HarnessProviderFrame(
                provider_key="openai",
                model="m",
                base_url=base_url,
                credential_source="user",
                local=False,
                keyless=False,
            )

    def test_frame_rejects_unknown_credential_source(self) -> None:
        with pytest.raises(HarnessContractError):
            HarnessProviderFrame(
                provider_key="openai",
                model="m",
                base_url="https://x",
                credential_source="root",
                local=False,
                keyless=False,
            )


class TestTurnRequest:
    def test_request_hides_credential_from_repr(self) -> None:
        request = _turn_request()
        assert CREDENTIAL not in repr(request)
        assert CREDENTIAL not in repr(HarnessAccounting(1, 0, HarnessOutcome.SUCCEEDED))

    def test_request_rejects_empty_user(self) -> None:
        with pytest.raises(HarnessContractError):
            HarnessTurnRequest(user_id="", message="m")

    def test_request_rejects_overlong_user(self) -> None:
        with pytest.raises(HarnessContractError):
            HarnessTurnRequest(user_id="u" * 257, message="m")

    def test_request_rejects_non_mapping_transport(self) -> None:
        with pytest.raises(HarnessContractError):
            HarnessTurnRequest(user_id="u", message="m", transport="nope")


class TestAccounting:
    def test_accounting_rejects_negative_attempts(self) -> None:
        with pytest.raises(HarnessContractError):
            HarnessAccounting(-1, 0, HarnessOutcome.SUCCEEDED)

    def test_accounting_rejects_negative_elapsed(self) -> None:
        with pytest.raises(HarnessContractError):
            HarnessAccounting(1, -1, HarnessOutcome.SUCCEEDED)

    def test_accounting_rejects_negative_tokens(self) -> None:
        with pytest.raises(HarnessContractError):
            HarnessAccounting(1, 0, HarnessOutcome.SUCCEEDED, total_tokens=-1)

    def test_accounting_rejects_credential_field_names(self) -> None:
        with pytest.raises(HarnessContractError):
            HarnessAccounting(
                1, 0, HarnessOutcome.SUCCEEDED, detail={"api_key": "value"}
            )

    def test_accounting_rejects_key_tokens_in_values(self) -> None:
        with pytest.raises(HarnessContractError):
            HarnessAccounting(
                1,
                0,
                HarnessOutcome.SUCCEEDED,
                detail={"note": "sk-" + "a" * 30},
            )

    def test_assert_credential_free_rejects_request_credential_echo(self) -> None:
        with pytest.raises(HarnessContractError):
            assert_credential_free(
                {"note": f"used {CREDENTIAL}"}, credential=CREDENTIAL
            )

    def test_assert_credential_free_rejects_plain_credential_echo(self) -> None:
        plain = "plain-conformance-credential-value"
        with pytest.raises(HarnessContractError, match="echo"):
            assert_credential_free({"note": f"used {plain}"}, credential=plain)

    def test_assert_credential_free_allows_clean_detail(self) -> None:
        assert_credential_free({"runner_outcome": "success"}, credential=CREDENTIAL)

    def test_empty_surface(self) -> None:
        surface = empty_surface()
        assert surface.tool_count == 0 and surface.agent_count == 0


class TestDeploymentValidation:
    def test_deployment_requires_every_seam(self) -> None:
        harness = StandardLLMHarness()
        identity = harness.identity
        with pytest.raises(HarnessContractError, match="lifecycle"):
            HarnessDeployment(
                identity=identity,
                lifecycle=None,
                tools=harness,
                provider=harness,
                accounting=harness,
            )
        with pytest.raises(HarnessContractError, match="tools"):
            HarnessDeployment(
                identity=identity,
                lifecycle=harness,
                tools=None,
                provider=harness,
                accounting=harness,
            )
        with pytest.raises(HarnessContractError, match="provider"):
            HarnessDeployment(
                identity=identity,
                lifecycle=harness,
                tools=harness,
                provider=None,
                accounting=harness,
            )
        with pytest.raises(HarnessContractError, match="accounting"):
            HarnessDeployment(
                identity=identity,
                lifecycle=harness,
                tools=harness,
                provider=harness,
                accounting=None,
            )

    def test_deployment_rejects_seam_missing_members(self) -> None:
        class IncompleteLifecycle:
            async def start(self, request): ...

        with pytest.raises(HarnessContractError, match="await_result"):
            HarnessDeployment(
                identity=HarnessIdentity("runner", "profile", "1.0.0"),
                lifecycle=IncompleteLifecycle(),
                tools=StandardLLMHarness(),
                provider=StandardLLMHarness(),
                accounting=StandardLLMHarness(),
            )

    def test_deployment_rejects_non_identity(self) -> None:
        with pytest.raises(HarnessContractError, match="identity"):
            validate_deployment(HarnessDeployment.__new__(HarnessDeployment))

    def test_seam_protocols_are_runtime_checkable(self) -> None:
        harness = StandardLLMHarness()
        assert isinstance(harness, LifecycleSeam)
        assert isinstance(harness, ToolSeam)
        assert isinstance(harness, ProviderSeam)
        assert isinstance(harness, AccountingSeam)


class TestCatalog:
    def test_default_catalog_exactly_the_shipped_runners(self) -> None:
        catalog = default_catalog()
        assert [d.identity.harness_id for d in catalog.deployments] == [
            "standard_openai_compatible",
            "typesafe_jev_latest",
        ]
        for deployment in catalog.deployments:
            assert deployment.default == (
                deployment.identity.harness_id == "standard_openai_compatible"
            )
            validate_deployment(deployment)

    def test_catalog_rejects_duplicates(self) -> None:
        with pytest.raises(HarnessContractError, match="duplicate"):
            HarnessCatalog(
                deployments=(
                    standard_deployment(default=True),
                    standard_deployment(),
                )
            )

    def test_catalog_requires_exactly_one_default(self) -> None:
        with pytest.raises(HarnessContractError, match="exactly one default"):
            HarnessCatalog(deployments=(standard_deployment(), typesafe_deployment()))
        with pytest.raises(HarnessContractError, match="exactly one default"):
            HarnessCatalog(
                deployments=(
                    standard_deployment(default=True),
                    typesafe_deployment(default=True),
                )
            )

    def test_catalog_rejects_unknown_lookup(self) -> None:
        with pytest.raises(HarnessContractError, match="no harness deployment"):
            default_catalog().deployment("missing")

    def test_catalog_rejects_foreign_contract_version(self) -> None:
        stale_identity = HarnessIdentity(
            "standard_openai_compatible", "standard", "1.0.0"
        )
        object.__setattr__(stale_identity, "contract_version", "0")
        stale = HarnessDeployment.__new__(HarnessDeployment)
        object.__setattr__(stale, "identity", stale_identity)
        object.__setattr__(stale, "lifecycle", StandardLLMHarness())
        object.__setattr__(stale, "tools", StandardLLMHarness())
        object.__setattr__(stale, "provider", StandardLLMHarness())
        object.__setattr__(stale, "accounting", StandardLLMHarness())
        object.__setattr__(stale, "default", True)
        with pytest.raises(HarnessContractError, match="targets contract"):
            HarnessCatalog(deployments=(stale, typesafe_deployment()))
        with pytest.raises(HarnessContractError, match="targets contract"):
            validate_catalog_deployment(stale)
        with pytest.raises(HarnessContractError, match="contract"):
            HarnessIdentity("runner", "profile", "1.0.0", contract_version="0")


class TestProfileSelection:
    def test_selection_unavailable_with_flag_off(self) -> None:
        catalog = default_catalog()
        with pytest.raises(HarnessSelectionUnavailable, match="unavailable"):
            catalog.select("standard_openai_compatible", flags=Flags())

    def test_selection_unavailable_without_qualified_rollout(self) -> None:
        catalog = default_catalog()
        with pytest.raises(HarnessSelectionUnavailable, match="qualified"):
            catalog.select("typesafe_jev_latest", flags=Flags(**{ROLLOUT_FLAG: True}))

    def test_selection_available_with_flag_and_rollout_record(self) -> None:
        catalog = HarnessCatalog(
            deployments=default_catalog().deployments, qualified_rollout=True
        )
        selected = catalog.select(
            "typesafe_jev_latest", flags=Flags(**{ROLLOUT_FLAG: True})
        )
        assert selected.identity.harness_id == "typesafe_jev_latest"

    def test_selection_fails_closed_on_broken_flag_source(self) -> None:
        class Broken:
            def is_enabled(self, flag: str) -> bool:
                raise RuntimeError("flag registry unavailable")

        with pytest.raises(HarnessSelectionUnavailable):
            default_catalog().select("standard_openai_compatible", flags=Broken())

    def test_rollout_flag_defaults_off_in_the_registry(self, monkeypatch) -> None:
        monkeypatch.delenv(ROLLOUT_ENV_VAR, raising=False)
        from shared.feature_flags import flags as global_flags

        monkeypatch.setattr(global_flags, "_flags", {})
        assert global_flags.is_enabled(ROLLOUT_FLAG) is False
        monkeypatch.setenv(ROLLOUT_ENV_VAR, "true")
        from shared.feature_flags import FeatureFlags

        assert FeatureFlags()._flags[ROLLOUT_FLAG] is True


class TestConformance:
    @pytest.mark.parametrize(
        "deployment",
        [
            typesafe_deployment(_typesafe_harness()),
            standard_deployment(StandardLLMHarness()),
        ],
        ids=["typesafe_jev_latest", "standard_openai_compatible"],
    )
    async def test_shipped_deployments_pass_conformance(
        self, deployment: HarnessDeployment
    ) -> None:
        report = await run_conformance(deployment)
        assert isinstance(report, ConformanceReport)
        assert report.passed, report.failures
        assert report.identity.harness_id == deployment.identity.harness_id
        assert {result.name for result in report.results} == {
            "lifecycle_success",
            "denial_without_credentials",
            "cancellation_midflight",
            "transient_failure_recovery",
            "nontransient_failure",
            "accounting_redaction",
            "tool_surface_bounded",
            "provider_frames_declared",
        }

    async def test_conformance_reports_assertion_failures(self) -> None:
        base = standard_deployment(StandardLLMHarness())

        class DenyingLifecycle:
            def __init__(self, inner) -> None:
                self._inner = inner

            async def start(self, request):
                return await self._inner.start(request)

            async def await_result(self, execution):
                result = await self._inner.await_result(execution)
                if result.outcome is not HarnessOutcome.SUCCEEDED:
                    return result
                return replace(result, outcome=HarnessOutcome.DENIED, decision=None)

            async def cancel(self, execution):
                return await self._inner.cancel(execution)

        deployment = replace(base, lifecycle=DenyingLifecycle(base.lifecycle))
        report = await run_conformance(deployment)
        assert not report.passed
        failed_names = [result.name for result in report.results if not result.passed]
        assert "lifecycle_success" in failed_names
        assert "denial_without_credentials" not in failed_names

    async def test_conformance_reports_scenario_crashes(self) -> None:
        class BrokenTools:
            def surface(self, request):
                raise RuntimeError("surface crashed")

        base = standard_deployment(StandardLLMHarness())
        deployment = replace(base, tools=BrokenTools())
        report = await run_conformance(deployment)
        assert not report.passed
        crashed = [r for r in report.results if r.name == "tool_surface_bounded"]
        assert crashed and "RuntimeError" in crashed[0].detail

    def test_conformance_rejects_unregistered_harness(self) -> None:
        stranger = replace(
            standard_deployment(StandardLLMHarness()),
            identity=HarnessIdentity("stranger_runner", "profile", "1.0.0"),
        )
        with pytest.raises(HarnessContractError, match="no conformance scripting"):
            conformance._request(stranger, "success")

    def test_conformance_rejects_unknown_scripts(self) -> None:
        with pytest.raises(HarnessContractError, match="TypeSafe"):
            conformance._typesafe_request("bogus")
        with pytest.raises(HarnessContractError, match="standard-LLM"):
            conformance._standard_request("bogus")

    async def test_conformance_failures_property_lists_failures(self) -> None:
        report = await run_conformance(typesafe_deployment(_typesafe_harness()))
        assert report.passed
        assert report.failures == ()


class TestTypeSafeAdapter:
    async def test_success_maps_decision_and_accounting(self) -> None:
        result, execution, harness = await _run_typesafe("success")
        assert result.outcome is HarnessOutcome.SUCCEEDED
        assert result.decision is not None
        assert result.skip_reason is None
        assert result.accounting.attempts == 1
        assert result.accounting.credential_outcome == "valid"
        assert result.accounting.surface_tools == 1
        assert result.accounting.surface_agents == 1
        assert result.accounting.detail == {"runner_outcome": "success"}
        assert execution.running is False

    async def test_flag_disabled_preserves_existing_skip(self) -> None:
        result, _execution, _harness = await _run_typesafe(
            "success",
            harness=_typesafe_harness(flags=Flags(**{"typesafe_routing": False})),
        )
        assert result.outcome is HarnessOutcome.SKIPPED
        assert result.skip_reason == "skipped_disabled"
        assert result.decision is None

    async def test_missing_key_preserves_existing_skip(self) -> None:
        result, _execution, _harness = await _run_typesafe("success", api_key=None)
        assert result.outcome is HarnessOutcome.SKIPPED
        assert result.skip_reason == "skipped_no_key"
        assert result.accounting.credential_outcome is None

    async def test_empty_request_preserves_existing_skip(self) -> None:
        harness = _typesafe_harness()
        request = _turn_request(
            message="", agents=(), tools=(), transport=_typesafe_transport("success")
        )
        execution = await harness.start(request)
        result = await harness.await_result(execution)
        assert result.outcome is HarnessOutcome.SKIPPED
        assert result.skip_reason == "skipped_no_key"

    async def test_auth_failure_maps_rejected_credential(self) -> None:
        result, _execution, _harness = await _run_typesafe("auth_failure")
        assert result.outcome is HarnessOutcome.FALLBACK
        assert result.accounting.credential_outcome == "rejected"
        assert result.accounting.detail == {"runner_outcome": "fallback_nontransient"}

    async def test_transient_exhaustion_maps_fallback(self) -> None:
        result, _execution, _harness = await _run_typesafe("transient_failure")
        assert result.outcome is HarnessOutcome.FALLBACK
        assert result.accounting.attempts == 3
        assert result.accounting.credential_outcome == "unavailable"

    async def test_transport_rejects_unknown_keys(self) -> None:
        harness = _typesafe_harness()
        with pytest.raises(HarnessContractError, match="unknown TypeSafe transport"):
            await harness.start(_turn_request(transport={"surprise": 1}))

    async def test_transport_client_is_used(self) -> None:
        harness = _typesafe_harness()
        fake = FakeTypeSafeClient(default=high_confidence(AGENT_ID, TOOL_NAME))
        request = _turn_request(transport={"client": fake})
        execution = await harness.start(request)
        result = await harness.await_result(execution)
        assert result.outcome is HarnessOutcome.SUCCEEDED
        assert fake.call_count == 1

    async def test_cancel_terminates_running_execution(self) -> None:
        harness = _typesafe_harness()
        request = _turn_request(transport=_typesafe_transport("slow"))
        execution = await harness.start(request)
        await asyncio.sleep(0)
        assert execution.running is True
        result = await harness.cancel(execution)
        assert result.outcome is HarnessOutcome.CANCELLED
        assert result.decision is None
        assert result.accounting.detail == {"runner_outcome": "cancelled"}
        assert execution.running is False

    async def test_cancel_after_completion_returns_real_outcome(self) -> None:
        harness = _typesafe_harness()
        request = _turn_request(transport=_typesafe_transport("success"))
        execution = await harness.start(request)
        await asyncio.wait({execution.task})
        result = await harness.cancel(execution)
        assert result.outcome is HarnessOutcome.SUCCEEDED
        assert result.decision is not None

    async def test_cancel_terminal_execution_is_idempotent(self) -> None:
        harness = _typesafe_harness()
        request = _turn_request(transport=_typesafe_transport("success"))
        execution = await harness.start(request)
        await asyncio.wait({execution.task})
        first = await harness.cancel(execution)
        second = await harness.cancel(execution)
        assert first.outcome is HarnessOutcome.SUCCEEDED
        assert second.outcome is HarnessOutcome.SUCCEEDED

    async def test_await_result_on_cancelled_task_maps_cancelled(self) -> None:
        harness = _typesafe_harness()
        request = _turn_request(transport=_typesafe_transport("slow"))
        execution = await harness.start(request)
        execution.task.cancel()
        await asyncio.wait({execution.task})
        result = await harness.await_result(execution)
        assert result.outcome is HarnessOutcome.CANCELLED

    async def test_await_result_propagates_own_cancellation(self) -> None:
        harness = _typesafe_harness()
        request = _turn_request(transport=_typesafe_transport("slow"))
        execution = await harness.start(request)
        await asyncio.sleep(0)
        awaiter = asyncio.ensure_future(harness.await_result(execution))
        await asyncio.sleep(0)
        awaiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await awaiter
        await harness.cancel(execution)

    async def test_cancelled_runner_outcome_maps_to_contract_cancelled(self) -> None:
        from orchestrator.typesafe_routing.runner import RoutingOutcome

        harness = _typesafe_harness()
        request = _turn_request(transport=_typesafe_transport("success"))
        execution = await harness.start(request)
        mapped = harness._map_routing_outcome(
            execution, RoutingOutcome(outcome=Outcome.CANCELLED)
        )
        assert mapped.outcome is HarnessOutcome.CANCELLED

    def test_surface_bounds_descriptions_and_filters_unbacked_agents(self) -> None:
        harness = _typesafe_harness()
        request = _turn_request(
            agents=(
                HarnessAgentOption(AGENT_ID, "Agent", "x" * 500),
                HarnessAgentOption("idle-agent", "Idle", "no tools"),
            ),
            tools=(HarnessToolOption(TOOL_NAME, AGENT_ID, "y" * 500),),
        )
        surface = harness.surface(request)
        assert surface.agent_count == 1
        assert surface.tool_count == 1
        assert all(len(option.description) <= 200 for option in surface.tools)
        assert all(len(option.description) <= 200 for option in surface.agents)
        assert surface.max_request_chars == 4000

    async def test_surface_message_bound_applies_to_dispatch(self) -> None:
        harness = _typesafe_harness()
        fake = FakeTypeSafeClient(default=high_confidence(AGENT_ID, TOOL_NAME))
        request = _turn_request(message="m" * 5000, transport={"client": fake})
        execution = await harness.start(request)
        await harness.await_result(execution)
        sent_state = fake.calls[0].state
        assert len(str(sent_state.get("current_request"))) <= 4000

    def test_provider_frames_declare_jev(self) -> None:
        harness = _typesafe_harness()
        frames = harness.provider_frames()
        assert len(frames) == 1
        frame = frames[0]
        assert frame.provider_key == "typesafe_jev_latest"
        assert frame.model == "jev-latest"
        assert frame.base_url == "https://api.typesafe.ai"
        assert frame.credential_source == "user"
        assert frame.local is False and frame.keyless is False

    def test_provider_frames_reject_non_url_base(self) -> None:
        harness = _typesafe_harness(base_url="not-a-url")
        with pytest.raises(HarnessContractError, match="http"):
            harness.provider_frames()

    async def test_accounting_refuses_credential_material(self) -> None:
        harness = _typesafe_harness()
        with pytest.raises(HarnessContractError, match="credential"):
            harness.accounting(
                outcome=HarnessOutcome.SUCCEEDED,
                attempts=1,
                started_monotonic=harness.clock(),
                surface=empty_surface(),
                detail={"echo": CREDENTIAL},
                credential=CREDENTIAL,
            )

    def test_deployment_advertises_complete_seams(self) -> None:
        deployment = _typesafe_harness().deployment()
        validate_deployment(deployment)
        assert deployment.default is False
        assert isinstance(deployment.lifecycle, LifecycleSeam)


class TestStandardAdapter:
    async def test_success_maps_usage_accounting(self) -> None:
        async def _invoke():
            return InvocationResult(
                "succeeded", 1, "completion", {"total_tokens": 11}, 11, True
            )

        harness = StandardLLMHarness()
        request = _turn_request(transport={"invoke": _invoke})
        execution = await harness.start(request)
        result = await harness.await_result(execution)
        assert result.outcome is HarnessOutcome.SUCCEEDED
        assert result.decision == ("completion", {"total_tokens": 11})
        assert result.accounting.total_tokens == 11
        assert result.accounting.attempts == 1
        assert result.accounting.credential_outcome is None

    async def test_usage_tokens_from_object_and_invalid(self) -> None:
        from orchestrator.harness.standard_llm_adapter import _usage_tokens

        class Usage:
            total_tokens = 5

        assert _usage_tokens(Usage()) == 5
        assert _usage_tokens({"total_tokens": 3}) == 3
        assert _usage_tokens(None) is None
        assert _usage_tokens({"total_tokens": "many"}) is None
        assert _usage_tokens({"total_tokens": True}) is None

    async def test_denied_when_invocation_reports_no_credentials(self) -> None:
        async def _deny():
            return InvocationResult("denied", 0, credential_outcome="unavailable")

        harness = StandardLLMHarness()
        request = _turn_request(transport={"invoke": _deny})
        execution = await harness.start(request)
        result = await harness.await_result(execution)
        assert result.outcome is HarnessOutcome.DENIED
        assert result.decision is None
        assert result.accounting.credential_outcome == "unavailable"
        assert result.accounting.detail == {"runner_outcome": "denied_no_credentials"}

    async def test_failure_records_error_class_without_message(self) -> None:
        async def _fail():
            return InvocationResult("failed", 1, error_class="ValueError")

        harness = StandardLLMHarness()
        request = _turn_request(transport={"invoke": _fail})
        execution = await harness.start(request)
        result = await harness.await_result(execution)
        assert result.outcome is HarnessOutcome.FAILED
        assert result.error_class == "ValueError"
        assert result.accounting.detail == {"runner_outcome": "failed"}
        assert all("secret" not in value for value in result.accounting.detail.values())

    async def test_start_requires_an_invoke_callable(self) -> None:
        harness = StandardLLMHarness()
        with pytest.raises(HarnessContractError, match="invoke callable"):
            await harness.start(_turn_request())

    async def test_transport_rejects_unknown_keys(self) -> None:
        async def _invoke():
            return InvocationResult("succeeded", 1, "m")

        harness = StandardLLMHarness(invoke=_invoke)
        with pytest.raises(HarnessContractError, match="unknown standard-LLM"):
            await harness.start(_turn_request(transport={"client": object()}))

    async def test_constructor_invoke_is_used(self) -> None:
        calls: list[int] = []

        async def _invoke():
            calls.append(1)
            return InvocationResult("succeeded", 1, "m")

        harness = StandardLLMHarness(invoke=_invoke)
        execution = await harness.start(_turn_request())
        result = await harness.await_result(execution)
        assert result.outcome is HarnessOutcome.SUCCEEDED
        assert calls == [1]

    async def test_cancel_terminates_running_execution(self) -> None:
        async def _slow():
            await asyncio.Event().wait()

        harness = StandardLLMHarness()
        request = _turn_request(transport={"invoke": _slow})
        execution = await harness.start(request)
        await asyncio.sleep(0)
        result = await harness.cancel(execution)
        assert result.outcome is HarnessOutcome.CANCELLED
        assert result.accounting.attempts == 0
        assert execution.running is False

    async def test_await_result_propagates_own_cancellation(self) -> None:
        async def _slow():
            await asyncio.Event().wait()

        harness = StandardLLMHarness()
        request = _turn_request(transport={"invoke": _slow})
        execution = await harness.start(request)
        await asyncio.sleep(0)
        awaiter = asyncio.ensure_future(harness.await_result(execution))
        await asyncio.sleep(0)
        awaiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await awaiter
        await harness.cancel(execution)

    async def test_await_result_on_cancelled_task_maps_cancelled(self) -> None:
        async def _slow():
            await asyncio.Event().wait()

        harness = StandardLLMHarness()
        request = _turn_request(transport={"invoke": _slow})
        execution = await harness.start(request)
        execution.task.cancel()
        await asyncio.wait({execution.task})
        result = await harness.await_result(execution)
        assert result.outcome is HarnessOutcome.CANCELLED
        assert result.decision is None

    async def test_cancel_after_completion_returns_real_outcome(self) -> None:
        async def _invoke():
            return InvocationResult("succeeded", 1, "m")

        harness = StandardLLMHarness()
        request = _turn_request(transport={"invoke": _invoke})
        execution = await harness.start(request)
        await asyncio.wait({execution.task})
        result = await harness.cancel(execution)
        assert result.outcome is HarnessOutcome.SUCCEEDED

    def test_provider_frames_cover_server_owned_presets(self) -> None:
        frames = StandardLLMHarness().provider_frames()
        by_key = {frame.provider_key: frame for frame in frames}
        assert "custom" not in by_key
        assert by_key["openai"].base_url == "https://api.openai.com/v1"
        assert by_key["openai"].local is False
        assert by_key["ollama"].base_url == "http://localhost:11434/v1"
        assert by_key["ollama"].local is True and by_key["ollama"].keyless is True
        assert by_key["lmstudio"].local is True
        assert by_key["anthropic"].keyless is False

    def test_frame_for_provider_ignores_submitted_url_for_presets(self) -> None:
        frame = StandardLLMHarness().frame_for_provider(
            "openai", "https://evil.example/v1", key_present=True, model="gpt-x"
        )
        assert frame is not None
        assert frame.base_url == "https://api.openai.com/v1"
        assert frame.model == "gpt-x"
        assert frame.keyless is False

    def test_frame_for_provider_resolves_custom_submitted_url(self) -> None:
        harness = StandardLLMHarness()
        frame = harness.frame_for_provider(
            "custom", "http://localhost:9999/v1", key_present=False
        )
        assert frame is not None
        assert frame.base_url == "http://localhost:9999/v1"
        assert frame.local is True
        assert frame.keyless is True

    def test_frame_for_provider_without_url_or_unknown_key(self) -> None:
        harness = StandardLLMHarness()
        assert harness.frame_for_provider("custom", "") is None
        assert harness.frame_for_provider("not-a-preset") is None

    async def test_accounting_records_surface_sizes(self) -> None:
        async def _invoke():
            return InvocationResult("succeeded", 1, "m")

        harness = StandardLLMHarness()
        request = _turn_request(transport={"invoke": _invoke})
        surface = harness.surface(request)
        assert surface.tool_count == 1 and surface.agent_count == 1
        assert surface.max_request_chars == 0

    def test_deployment_advertises_complete_seams(self) -> None:
        deployment = StandardLLMHarness().deployment(default=True)
        validate_deployment(deployment)
        assert deployment.default is True


class TestBehaviorPreservation:
    async def test_typesafe_circuit_still_opens_after_auth_failure(self) -> None:
        harness = _typesafe_harness()
        first = await _run_typesafe("auth_failure", harness=harness)
        assert first[0].outcome is HarnessOutcome.FALLBACK
        request = _turn_request(transport=_typesafe_transport("success"))
        execution = await harness.start(request)
        result = await harness.await_result(execution)
        assert result.outcome is HarnessOutcome.SKIPPED
        assert result.skip_reason == "skipped_circuit"

    async def test_circuit_isolated_between_users(self) -> None:
        harness = _typesafe_harness()
        await _run_typesafe("auth_failure", harness=harness)
        other = _turn_request(
            user_id="another-user", transport=_typesafe_transport("success")
        )
        execution = await harness.start(other)
        result = await harness.await_result(execution)
        assert result.outcome is HarnessOutcome.SUCCEEDED

    async def test_existing_tool_surface_narrowing_is_unchanged(self) -> None:
        harness = _typesafe_harness()
        fake = FakeTypeSafeClient(default=high_confidence(AGENT_ID, TOOL_NAME))
        request = _turn_request(
            tools=(
                HarnessToolOption(TOOL_NAME, AGENT_ID, "kept"),
                HarnessToolOption("other-agent__tool", "other-agent", "kept"),
            ),
            agents=(
                HarnessAgentOption(AGENT_ID, "Agent", "kept"),
                HarnessAgentOption("empty-agent", "Empty", "no tools"),
            ),
            transport={"client": fake},
        )
        surface = harness.surface(request)
        agent_ids = {option.agent_id for option in surface.agents}
        assert agent_ids == {AGENT_ID}
        execution = await harness.start(request)
        result = await harness.await_result(execution)
        assert result.outcome is HarnessOutcome.SUCCEEDED
        asked_agents = str(fake.calls[0].state)
        assert "empty-agent" not in asked_agents

    def test_accounting_seam_defaults_are_explicit(self) -> None:
        harness = _typesafe_harness()
        record = harness.accounting(
            outcome=HarnessOutcome.SUCCEEDED,
            attempts=2,
            started_monotonic=harness.clock(),
            surface=HarnessToolSurface(agents=(), tools=(), max_request_chars=0),
        )
        assert record.outcome is HarnessOutcome.SUCCEEDED
        assert record.attempts == 2
        assert record.credential_outcome is None
        assert record.total_tokens is None
        assert record.detail == {}
        assert isinstance(harness, AccountingSeam)

    def test_outcome_enum_covers_runner_vocabularies(self) -> None:
        runner_values = {outcome.value for outcome in Outcome}
        assert "success" in runner_values
        assert "cancelled" in runner_values
        mapped = {
            Outcome.SUCCESS: HarnessOutcome.SUCCEEDED,
            Outcome.FALLBACK_DEADLINE: HarnessOutcome.FALLBACK,
            Outcome.SKIPPED_CIRCUIT: HarnessOutcome.SKIPPED,
            Outcome.CANCELLED: HarnessOutcome.CANCELLED,
        }
        assert len(mapped) == 4
